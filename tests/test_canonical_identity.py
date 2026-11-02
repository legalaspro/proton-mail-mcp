"""P-A: every tool reports one canonical (folder, UIDVALIDITY, UID) per message.

Bridge exposes a message in its single real folder and again in views (All Mail,
Starred, Labels/...), each with its own UID space. Search, threads, previews,
moves and rules must all name the real folder's handle; a view handle is only an
alias. These tests drive the real imaplib client against the Bridge simulator.
"""

import json
import re
from pathlib import Path

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_cleanup_guard import setup as cleanup_setup
from test_mail import FakeImap, message
from test_mail import client as fake_client
from test_mail import settings as settings

from proton_mail_mcp import canonical, guarded
from proton_mail_mcp.canonical import listed_name, row_identity
from proton_mail_mcp.config import Settings
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.search_index import single_copy
from proton_mail_mcp.threading import MAX_THREAD_BODY_BYTES

KEYS = ("folder", "uidvalidity", "uid")
NEWS = "News <news@example.invalid>"
TWIN = "<twin@example.invalid>"


def triple(item):
    return tuple(item[key] for key in KEYS)


def unresolved_search(result, *, lookup_failed=False):
    assert result["messages"] == []
    assert result["partial"] is True
    assert result["next_cursor"]
    if lookup_failed:
        assert result["partial_reason"] == "canonical_unresolved"
    else:
        assert result["partial_reason"] == "unresolved_handles"


def handle(sim, folder, msg):
    return folder, str(sim.uidvalidity[folder]), str(sim.uid_of(folder, msg.pid))


def alias(sim, folder, msg):
    return dict(zip(KEYS, handle(sim, folder, msg)))


@pytest.fixture
def mailbox(tmp_path):
    sim = BridgeSim()
    client = make_client(sim, tmp_path)
    yield sim, client
    sim.close()
    assert not sim.errors and not sim.violations
    assert all(not worker.is_alive() for _, _, worker in sim.peers)


def seed(sim, **options):
    # Older archived mail takes the first All Mail UIDs, so the target's INBOX
    # UID differs from its All Mail UID, as in the reported 20321/47175 case.
    for index in range(5):
        sim.add(Msg(hdr(f"<archived-{index}@example.invalid>", subject=f"Old {index}"), "Archive"))
    target = sim.add(
        Msg(hdr("<target@example.invalid>", from_=NEWS, subject="Weekly digest"), **options)
    )
    assert sim.uid_of("INBOX", target.pid) != sim.uid_of("All Mail", target.pid)
    return target


def rules_service(client, folder="Folders/Reviewed"):
    path = Path(client.settings.action_log_path).parent / "synthetic-rules.json"
    rule = {"field": "from", "contains": "news@example.invalid", "folder": folder}
    path.write_text(json.dumps({"version": 1, "rules": [rule]}))
    return FolderRulesService(client.cleanup, path)


def test_inbox_and_all_mail_copies_report_one_handle_in_every_read_tool(mailbox):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    expected = handle(sim, "INBOX", target)

    inbox = client.search_mail(folder="INBOX", include_snippets=False)["messages"]
    assert [triple(item) for item in inbox] == [expected]
    assert "alias" not in inbox[0]

    thread = client.read_thread(target.message_id())
    assert thread["partial"] is False
    [read] = thread["messages"]
    assert triple(read) == expected
    assert read["alias"] == alias(sim, "All Mail", target)
    assert read["text"] == "Body text"

    # Searching a view finds the same message under that view's UID, but still
    # reports the real folder's handle and keeps the view handle as alias.
    for view in ("All Mail", "Labels/Review"):
        found = client.search_mail(folder=view, since="2026-10-01", include_snippets=False)
        [hit] = [item for item in found["messages"] if item["message_id"] == target.message_id()]
        assert triple(hit) == expected
        assert hit["alias"] == alias(sim, view, target)
        assert found["partial"] is False

    preview = client.preview_action("unsubscribe", ids=[target.message_id()])
    [sample] = preview["samples"]
    assert triple(sample) == expected and "alias" not in sample
    assert mutations(sim) == []


def test_previews_bind_the_canonical_handle_that_search_and_thread_report(mailbox):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    expected = handle(sim, "INBOX", target)
    [read] = client.read_thread(target.message_id())["messages"]
    assert triple(read) == expected

    for selector in ({"ids": [target.message_id()]}, {"query": {"folder": "INBOX"}}):
        client.cleanup.tokens._grants.clear()
        preview = client.preview_action(
            "move_to_folder", params={"folder": "Folders/Reviewed"}, **selector
        )
        assert preview["token"] and preview["count"] == 1
        [sample] = preview["samples"]
        assert triple(sample) == expected and "alias" not in sample
        # The confirmation token binds exactly that canonical handle.
        [grant] = list(client.cleanup.tokens._grants.values())
        assert [triple(row) for row in grant.messages] == [expected]

    # A label action acts on the label's handle; its sample still names the
    # message's real folder and keeps the label handle as alias.
    client.cleanup.tokens._grants.clear()
    preview = client.preview_action(
        "remove_label", ids=[target.message_id()], params={"label": "Review"}
    )
    assert preview["token"]
    [sample] = preview["samples"]
    assert triple(sample) == expected
    assert sample["alias"] == alias(sim, "Labels/Review", target)
    [grant] = list(client.cleanup.tokens._grants.values())
    [row] = grant.messages
    assert triple(row) == handle(sim, "Labels/Review", target)
    assert expected in {triple(member) for member in row["memberships"]}
    assert mutations(sim) == []


def test_move_executes_the_canonical_handle_and_reads_follow_the_message(mailbox):
    sim, client = mailbox
    target = seed(sim)
    expected = handle(sim, "INBOX", target)
    [searched] = client.search_mail(folder="INBOX", include_snippets=False)["messages"]
    assert triple(searched) == expected
    preview = client.preview_action(
        "move_to_folder", ids=[target.message_id()], params={"folder": "Folders/Reviewed"}
    )
    moved = client.move_to_folder(preview["token"])
    assert moved["count"] == 1 and not moved["partial"]
    [entry] = moved["messages"]
    assert (entry["source_folder"], entry["uidvalidity_before"], entry["uid_before"]) == expected
    after = ("Folders/Reviewed", entry["uidvalidity_after"], entry["uid_after"])
    assert after == handle(sim, "Folders/Reviewed", target)

    # The moved message has a new canonical handle; every read tool agrees on it.
    destination = client.search_mail(folder="Folders/Reviewed", include_snippets=False)
    assert [triple(item) for item in destination["messages"]] == [after]
    [read] = client.read_thread(target.message_id())["messages"]
    assert triple(read) == after
    assert read["alias"] == alias(sim, "All Mail", target)
    assert client.search_mail(folder="INBOX", include_snippets=False)["messages"] == []


def test_rules_preview_and_apply_use_the_canonical_handle(mailbox):
    sim, client = mailbox
    target = seed(sim)
    expected = handle(sim, "INBOX", target)
    [read] = client.read_thread(target.message_id())["messages"]
    assert triple(read) == expected

    rules = rules_service(client)
    preview = rules.preview_rules()
    assert preview["token"] and preview["count"] == 1
    [sample] = preview["rules"][0]["samples"]
    assert triple(sample) == expected and "alias" not in sample
    [grant] = list(client.cleanup.tokens._grants.values())
    assert [triple(row) for row in grant.messages] == [expected]

    applied = rules.apply_rules(preview["token"])
    assert applied["count"] == 1 and not applied["partial"]
    [entry] = applied["messages"]
    assert (entry["source_folder"], entry["uidvalidity_before"], entry["uid_before"]) == expected
    [read] = client.read_thread(target.message_id())["messages"]
    [searched] = client.search_mail(folder="Folders/Reviewed", include_snippets=False)["messages"]
    assert triple(read) == triple(searched) == handle(sim, "Folders/Reviewed", target)


def test_reused_message_id_copies_resolve_to_their_own_real_folders(mailbox):
    sim, client = mailbox
    # GitHub reuses Message-IDs for different notifications in one conversation.
    identifier = "<pull/63/review@github.example>"
    opened = sim.add(Msg(hdr(identifier, subject="PR opened"), folder="INBOX"))
    merged = sim.add(Msg(hdr(identifier, subject="PR merged"), folder="Archive"))
    found = client.search_mail(folder="All Mail", since="2026-10-01", include_snippets=False)
    assert {item["subject"]: triple(item) for item in found["messages"]} == {
        "PR opened": handle(sim, "INBOX", opened),
        "PR merged": handle(sim, "Archive", merged),
    }
    assert {item["subject"]: item["alias"] for item in found["messages"]} == {
        "PR opened": alias(sim, "All Mail", opened),
        "PR merged": alias(sim, "All Mail", merged),
    }
    # Threads keep one entry per Message-ID; it names the copy actually read.
    [read] = client.read_thread(identifier)["messages"]
    assert read["alias"] == alias(sim, "All Mail", opened)
    assert triple(read) == handle(sim, "INBOX", opened)


def test_indistinguishable_copies_are_never_guessed(mailbox):
    sim, client = mailbox
    first = sim.add(Msg(hdr("<twin@example.invalid>"), folder="INBOX"))
    sim.add(Msg(hdr("<twin@example.invalid>"), folder="Archive"))
    thread = client.read_thread("<twin@example.invalid>")
    [read] = thread["messages"]
    assert triple(read) == (None, None, None)
    assert read["alias"] == alias(sim, "All Mail", first)
    assert thread["partial"] is False
    found = client.search_mail(folder="All Mail", since="2026-10-01", include_snippets=False)
    unresolved_search(found)
    # A real folder's own handle is canonical regardless of copies elsewhere.
    inbox = client.search_mail(folder="INBOX", include_snippets=False)["messages"]
    assert [triple(item) for item in inbox] == [handle(sim, "INBOX", first)]


def test_a_view_only_twin_stops_the_real_copy_from_being_claimed(mailbox):
    sim, client = mailbox
    # Identical messages, one without any real folder: one real copy shares the
    # fingerprint, but All Mail holds two such messages, so neither is guessed.
    orphan = sim.add(Msg(hdr("<twin@example.invalid>"), folder=None))
    sim.add(Msg(hdr("<twin@example.invalid>"), folder="INBOX"))
    [read] = client.read_thread("<twin@example.invalid>")["messages"]
    assert read["alias"] == alias(sim, "All Mail", orphan)
    assert triple(read) == (None, None, None)
    found = client.search_mail(folder="All Mail", since="2026-10-01", include_snippets=False)
    unresolved_search(found)


def test_message_without_a_real_folder_keeps_only_its_alias(mailbox):
    sim, client = mailbox
    orphan = sim.add(Msg(hdr("<orphan@example.invalid>"), folder=None, labels={"Labels/Keep"}))
    [read] = client.read_thread("<orphan@example.invalid>")["messages"]
    assert triple(read) == (None, None, None)
    assert read["alias"] == alias(sim, "All Mail", orphan)
    found = client.search_mail(folder="Labels/Keep", since="2026-10-01")
    unresolved_search(found)


def test_message_sent_to_yourself_reports_its_inbox_copy(mailbox):
    sim, client = mailbox
    sim.add(Msg(hdr("<other@example.invalid>"), folder="Sent"))
    mine = sim.add(Msg(hdr("<mine@example.invalid>"), folder="INBOX", labels={"Sent"}))
    assert handle(sim, "INBOX", mine) != handle(sim, "Sent", mine)

    # With nothing indexed yet, this is the live lookup over every real folder.
    thread = client.read_thread(mine.message_id())
    [read] = thread["messages"]
    assert triple(read) == handle(sim, "INBOX", mine)
    assert read["alias"] == alias(sim, "All Mail", mine)
    assert "canonical_reason" not in read and thread["partial"] is False

    found = client.search_mail(folder="All Mail", since="2026-10-01", include_snippets=False)
    [hit] = [item for item in found["messages"] if item["message_id"] == mine.message_id()]
    assert triple(hit) == handle(sim, "INBOX", mine)
    # A real folder's own handle is canonical regardless of copies elsewhere.
    sent = client.search_mail(folder="Sent", include_snippets=False)["messages"]
    [own] = [item for item in sent if item["message_id"] == mine.message_id()]
    assert triple(own) == handle(sim, "Sent", mine)
    assert mutations(sim) == []


@pytest.mark.parametrize("extra", ["Archive", "Spam", "Trash"])
def test_a_sent_copy_beside_another_folder_is_never_guessed(mailbox, extra):
    sim, client = mailbox
    mine = sim.add(Msg(hdr("<mine@example.invalid>"), folder=extra, labels={"Sent"}))
    [read] = client.read_thread(mine.message_id())["messages"]
    assert triple(read) == (None, None, None)
    assert read["canonical_reason"] == "no_single_copy"


@pytest.mark.parametrize(
    "folders,expected",
    [
        ([], None),
        (["Archive"], "Archive"),
        (["Sent"], "Sent"),
        (["Sent", "INBOX"], "INBOX"),
        (["inbox", "Sent"], "inbox"),
        (["INBOX", "Archive"], None),
        (["INBOX", "INBOX"], None),
        (["Sent", "Sent"], None),
        (["sent", "INBOX"], None),
        (["INBOX", "Sent", "Archive"], None),
        (["INBOX", "INBOX", "Sent"], None),
    ],
)
def test_single_copy_settles_only_one_copy_or_inbox_with_sent(folders, expected):
    copies = [canonical.handle(folder, "1", str(uid)) for uid, folder in enumerate(folders)]
    copy = single_copy(copies)
    assert (copy and copy["folder"]) == expected


def test_reason_says_why_each_read_has_no_canonical_handle(mailbox, monkeypatch):
    sim, client = mailbox
    placed = sim.add(Msg(hdr("<placed@example.invalid>")))
    orphan = sim.add(Msg(hdr("<orphan@example.invalid>"), folder=None))

    assert "canonical_reason" not in client.read_thread(placed.message_id())["messages"][0]
    [read] = client.read_thread(orphan.message_id())["messages"]
    assert read["canonical_reason"] == "no_single_copy"
    [direct] = client.read_thread(placed.message_id(), include_thread=False)["messages"]
    assert triple(direct) == (None, None, None)
    assert direct["canonical_reason"] == "lookup_skipped"

    def refuse(*args, **kwargs):
        raise MailError("Mailbox changed during canonical lookup")

    monkeypatch.setattr(canonical, "resolve_views", refuse)
    monkeypatch.setattr(canonical, "resolve_indexed_views", lambda *args, **kwargs: None)
    [failed] = client.read_thread(placed.message_id())["messages"]
    assert triple(failed) == (None, None, None)
    assert failed["canonical_reason"] == "lookup_unfinished"


def test_without_all_mail_the_reason_is_not_a_finished_lookup(settings):
    fake = FakeImap({"Labels/Keep": {"7": message(TWIN)}}, uidnext=True)
    [read] = fake_client(settings, fake).read_thread(TWIN)["messages"]
    assert triple(read) == (None, None, None)
    assert read["canonical_reason"] == "lookup_unfinished"


LOOKUP_FETCH = "(UID INTERNALDATE RFC822.SIZE ENVELOPE)"


def lookup_commands(commands):
    return [
        command
        for command in commands
        if re.search(r" ON [0-9]{2}-[A-Z][a-z]{2}-[0-9]{4}", command) or LOOKUP_FETCH in command
    ]


@pytest.mark.parametrize("copies", [("Archive",), ("Archive", "Spam")])
def test_without_all_mail_a_view_hit_is_never_resolved(tmp_path, copies):
    sim = BridgeSim(show_all_mail=False)
    client = make_client(sim, tmp_path)
    try:
        sim.add(Msg(hdr("<inbox@example.invalid>"), folder="INBOX"))
        target = sim.add(Msg(hdr("<kept@example.invalid>"), folder=copies[0], starred=True))
        for folder in copies[1:]:
            sim.add(Msg(hdr("<kept@example.invalid>"), folder=folder))
        # Thread lookup visits Starred before Archive and reads that view first.
        # Without All Mail nothing lists each message once, so no lookup could
        # tell this message from a Starred-only twin and none is attempted.
        thread = client.read_thread("<kept@example.invalid>")
        [read] = thread["messages"]
        assert triple(read) == (None, None, None)
        assert read["alias"] == alias(sim, "Starred", target)
        assert thread["partial"] is False
        assert lookup_commands(command for _, _, _, command in sim.commands) == []
        assert not sim.errors and not sim.violations
    finally:
        sim.close()


def test_without_all_mail_a_view_only_twin_never_takes_the_real_handle(settings):
    # Identical headers, INTERNALDATE and size, but equal-length different
    # bodies: the Starred orphan and the unstarred INBOX message are two messages.
    orphan = message(TWIN, sections={"1": b"ORPHAN"})
    inbox = message(TWIN, sections={"1": b"INBOX!"})
    fake = FakeImap(
        {"INBOX": {"7": inbox}, "Starred": {"70": orphan}}, folder_flags={"Starred": r"\Flagged"}
    )
    mailbox = fake_client(settings, fake)
    starred = mailbox.search_mail(folder="Starred", since="2026-10-01")
    unresolved_search(starred)
    assert not any(call[0] == "SEARCH" and "ON" in call for call in fake.calls)
    assert not any(call[0] == "FETCH" and call[2] == LOOKUP_FETCH for call in fake.calls)
    [own] = mailbox.search_mail(folder="INBOX", since="2026-10-01")["messages"]
    assert own["snippet"] == "INBOX!" and triple(own) == ("INBOX", "1", "7")


def test_view_renumbered_before_lookup_is_never_resolved_by_its_stale_uid(mailbox):
    sim, client = mailbox
    first = sim.add(Msg(hdr("<first@example.invalid>", subject="First"), folder="INBOX"))
    sim.add(Msg(hdr("<second@example.invalid>", subject="Second"), folder="Archive"))
    stale = alias(sim, "All Mail", first)
    opened = []

    def renumber(sid, selected, op, text):
        if op == "EXAMINE" and "All Mail" in text:
            opened.append(text)
            if len(opened) == 2:
                # A new UIDVALIDITY with reversed UIDs before the canonical lookup:
                # the stale UID now names the other message.
                sim.reset_uidvalidity("All Mail")

    sim.hooks.append(renumber)
    [read] = client.read_thread("<first@example.invalid>")["messages"]
    assert len(opened) == 2
    assert read["alias"] == stale
    assert triple(read) == (None, None, None)


def arrive_when_examined(sim, folder, msg):
    """Deliver ``msg`` as ``folder`` is first opened; return the openings seen."""
    opened = []

    def deliver(sid, selected, op, text):
        if op == "EXAMINE" and folder in text:
            opened.append(text)
            if len(opened) == 1:
                sim.add(msg)

    sim.hooks.append(deliver)
    return opened


@pytest.mark.parametrize("tool", ["read_thread", "label search"])
def test_twin_arriving_during_the_lookup_leaves_the_handle_unknown(mailbox, tool):
    sim, client = mailbox
    # Only the orphan exists when All Mail is scanned. An identical message,
    # with an equal-length different body, arrives as the lookup opens INBOX.
    orphan = sim.add(Msg(hdr(TWIN), folder=None, labels={"Labels/Keep"}, body=b"AAA\r\n"))
    opened = arrive_when_examined(sim, "INBOX", Msg(hdr(TWIN), folder="INBOX", body=b"BBB\r\n"))
    if tool == "read_thread":
        result = client.read_thread(TWIN)
        [item] = result["messages"]
        assert item["text"] == "AAA"
        view = "All Mail"
    else:
        result = client.search_mail(folder="Labels/Keep", since="2026-10-01")
        unresolved_search(result, lookup_failed=True)
    assert opened
    # All Mail no longer holds the one copy it was scanned with: never claimed.
    if tool == "read_thread":
        assert triple(item) == (None, None, None)
        assert item["alias"] == alias(sim, view, orphan)
    # A thread's null canonical fields say it all; a search page leaves the row out.
    assert result["partial"] is (tool != "read_thread")
    assert mutations(sim) == []


def test_twin_delivered_to_inbox_7_during_the_lookup_never_lends_its_handle(settings):
    # Review reproduction: the orphan is All Mail 70; the matching message
    # arrives at INBOX 7 and All Mail 71 when the lookup selects INBOX.
    orphan = message(TWIN, sections={"1": b"AAA"})
    twin = message(TWIN, sections={"1": b"BBB"})
    fake = FakeImap({"INBOX": {}, "All Mail": {"70": orphan}}, uidnext=True)
    deliveries = [("INBOX", "7", twin), ("All Mail", "71", twin)]
    select = fake.select

    def deliver(folder, *, readonly):
        if folder == '"INBOX"':
            for mailbox, uid, item in deliveries:
                fake.folders[mailbox][uid] = item
            deliveries.clear()
        return select(folder, readonly=readonly)

    fake.select = deliver
    mailbox = fake_client(settings, fake)
    thread = mailbox.read_thread(TWIN)
    assert not deliveries and fake.uidvalidity == "1"
    [read] = thread["messages"]
    assert read["text"] == "AAA"
    assert triple(read) == (None, None, None)
    assert read["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "70"}
    assert thread["partial"] is False
    [other] = mailbox.search_mail(folder="INBOX", since="2026-10-01")["messages"]
    assert triple(other) == ("INBOX", "1", "7") and other["snippet"] == "BBB"


def test_view_message_leaving_during_the_lookup_never_lends_its_twins_handle(mailbox):
    sim, client = mailbox
    # All Mail holds the labeled orphan and its identical INBOX twin, so the
    # label is never resolved. Deleting the orphan before All Mail is scanned
    # would leave the twin looking unique in All Mail and INBOX.
    orphan = sim.add(Msg(hdr(TWIN), folder=None, labels={"Labels/Keep"}, body=b"AAA\r\n"))
    sim.add(Msg(hdr(TWIN), folder="INBOX", body=b"BBB\r\n"))
    deleted = []

    def delete_orphan(sid, selected, op, text):
        if op == "EXAMINE" and "All Mail" in text and not deleted:
            with sim.lock:
                deleted.append(sim.messages.pop(orphan.pid))

    sim.hooks.append(delete_orphan)
    found = client.search_mail(folder="Labels/Keep", since="2026-10-01")
    assert deleted
    unresolved_search(found, lookup_failed=True)


def test_any_arrival_during_the_lookup_withholds_the_handle(mailbox):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    late = Msg(hdr("<late@example.invalid>", subject="Late"), folder="INBOX")
    opened = arrive_when_examined(sim, "INBOX", late)
    found = client.search_mail(folder="Labels/Review", since="2026-10-01", include_snippets=False)
    # A changed All Mail proves nothing about this message's copies.
    assert opened
    unresolved_search(found, lookup_failed=True)
    # Once nothing arrives during the lookup, the same search resolves it.
    again = client.search_mail(folder="Labels/Review", since="2026-10-01", include_snippets=False)
    [hit] = [item for item in again["messages"] if item["message_id"] == target.message_id()]
    assert triple(hit) == handle(sim, "INBOX", target) and again["partial"] is False


@pytest.mark.parametrize("uidnext", [False, True])
def test_mailboxes_without_uidnext_never_prove_a_lookup(settings, uidnext):
    root = message("<root@example.com>", sections={"1": b"Requested body"})
    fake = FakeImap({"INBOX": {"7": root}, "All Mail": {"70": root}}, uidnext=uidnext)
    thread = fake_client(settings, fake).read_thread("<root@example.com>")
    [read] = thread["messages"]
    assert read["text"] == "Requested body"
    assert read["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "70"}
    # Without UIDNEXT nothing shows that no twin arrived during the scans.
    assert triple(read) == (("INBOX", "1", "7") if uidnext else (None, None, None))
    assert thread["partial"] is False


SAME_ID = "<shared@example.invalid>"
# The label source's subject and an INBOX message's subject, both with the same
# Message-ID and Date, then whether All Mail is shown and the source protected.
# With All Mail, a repeated Message-ID excludes an unprotected source from the
# preview, so a protected one is shown as blocked instead.
SAME_ID_SOURCES = {
    "equal-size other message": ("Orphan", "Hello!", False, False),
    "different-size other message": ("Orphan copy", "Hello!", False, False),
    "identical twin": ("Hello!", "Hello!", False, False),
    "protected identical twin": ("Hello!", "Hello!", False, True),
    "equal-size other message with All Mail": ("Orphan", "Hello!", True, True),
    "identical twin with All Mail": ("Hello!", "Hello!", True, True),
}


@pytest.mark.parametrize("case", SAME_ID_SOURCES)
def test_label_previews_and_search_never_borrow_a_same_id_handle(tmp_path, monkeypatch, case):
    subject, other_subject, show_all_mail, protected = SAME_ID_SOURCES[case]
    sim = BridgeSim(show_all_mail=show_all_mail)
    client = make_client(sim, tmp_path)
    try:
        orphan = sim.add(Msg(hdr(SAME_ID, subject=subject), folder=None, labels={"Labels/Keep"}))
        other = sim.add(Msg(hdr(SAME_ID, subject=other_subject), folder="INBOX"))
        if protected:
            check = client.cleanup.policy.check
            monkeypatch.setattr(
                client.cleanup.policy,
                "check",
                lambda row: ["Protected sender"] if row["folder"] == "Labels/Keep" else check(row),
            )
        preview = client.preview_action(
            "add_label", query={"folder": "Labels/Keep"}, params={"label": "Review"}
        )
        found = client.search_mail(folder="Labels/Keep", since="2026-10-01", include_snippets=False)
        assert len(preview["blocked"]) == int(protected)
        label = alias(sim, "Labels/Keep", orphan)
        for item in [*preview["samples"], *preview["blocked"]]:
            assert item["subject"] == subject
            assert triple(item) == (None, None, None)
            assert item["alias"] == label
        assert len(preview["samples"]) == 1
        assert preview["partial"] is False
        unresolved_search(found)
        # The INBOX handle belongs to the other message.
        [inbox] = client.search_mail(folder="INBOX", include_snippets=False)["messages"]
        assert inbox["subject"] == other_subject
        assert triple(inbox) == handle(sim, "INBOX", other)
        assert mutations(sim) == []
        assert not sim.errors and not sim.violations
    finally:
        sim.close()


@pytest.mark.parametrize("action", ["add_label", "unsubscribe"])
def test_label_source_previews_report_the_handle_that_search_reports(mailbox, action):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Keep"})
    target.headers = (
        b"List-Unsubscribe: <https://news.example.com/unsubscribe>\r\n"
        b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n" + target.headers
    )
    params = {"label": "Review"} if action == "add_label" else None
    preview = client.preview_action(action, query={"folder": "Labels/Keep"}, params=params)
    found = client.search_mail(folder="Labels/Keep", since="2026-10-01", include_snippets=False)
    [sample] = preview["samples"]
    [hit] = found["messages"]
    assert triple(sample) == triple(hit) == handle(sim, "INBOX", target)
    assert sample["alias"] == hit["alias"] == alias(sim, "Labels/Keep", target)
    assert preview["token"] and preview["partial"] is False
    # The approval still binds the label source handle.
    [grant] = list(client.cleanup.tokens._grants.values())
    assert [triple(row) for row in grant.messages] == [handle(sim, "Labels/Keep", target)]
    assert mutations(sim) == []


def test_preview_lookup_failure_keeps_the_token_and_reports_partial(mailbox):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    lookup = []

    def refuse_lookup(sid, selected, op, text):
        # Refuse INBOX once the canonical lookup has begun its ON-day searches.
        if op == "SEARCH" and " ON " in text:
            lookup.append(text)
        if op == "EXAMINE" and "INBOX" in text and lookup:
            return ("NO", "synthetic refusal")

    sim.hooks.append(refuse_lookup)
    preview = client.preview_action(
        "remove_label", ids=[target.message_id()], params={"label": "Review"}
    )
    sim.hooks.remove(refuse_lookup)
    assert lookup
    [sample] = preview["samples"]
    assert triple(sample) == (None, None, None)
    assert sample["alias"] == alias(sim, "Labels/Review", target)
    # Only the displayed handle is unknown; the approval is unaffected.
    assert preview["token"] and preview["partial"] is True
    [grant] = list(client.cleanup.tokens._grants.values())
    assert [triple(row) for row in grant.messages] == [handle(sim, "Labels/Review", target)]
    assert mutations(sim) == []


@pytest.mark.parametrize("spent", [0.39, 0.41])
def test_label_samples_resolve_only_within_the_preview_budget(mailbox, monkeypatch, spent):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    budget = client.settings.preview_timeout or client.settings.call_timeout
    resolve = guarded.resolve_samples

    def after_approval(*args, started, **kwargs):
        # As if approval had taken this share of the preview budget.
        return resolve(*args, started=started - budget * spent, **kwargs)

    monkeypatch.setattr(guarded, "resolve_samples", after_approval)
    preview = client.preview_action(
        "remove_label", ids=[target.message_id()], params={"label": "Review"}
    )
    [sample] = preview["samples"]
    assert sample["alias"] == alias(sim, "Labels/Review", target)
    assert preview["token"]
    # A lookup costs about as much as approval, so it starts only if both fit
    # within the 80% preview budget that approval had to fit.
    if spent < 0.4:
        assert triple(sample) == handle(sim, "INBOX", target)
        assert preview["partial"] is False
    else:
        assert triple(sample) == (None, None, None)
        assert preview["partial"] is True
        assert lookup_commands(command for _, _, _, command in sim.commands) == []


def test_sample_identity_never_comes_from_memberships():
    real = {"INBOX", "Archive"}
    label = {"folder": "Labels/Review", "uidvalidity": "9", "uid": "4"}
    # A membership shares only a Message-ID with the row; it may be another
    # message. Previews resolve the alias through the lookup search uses.
    memberships = [
        {"message_id": "<a@example.invalid>", "folder": "INBOX", "uidvalidity": "1", "uid": "7"},
        {"message_id": "<a@example.invalid>", "folder": "All Mail", "uidvalidity": "3", "uid": "5"},
    ]
    unknown = dict.fromkeys(KEYS)
    assert row_identity({**label, "memberships": memberships}, real) == {
        **unknown,
        "alias": label,
    }
    # A real source row is canonical itself, whatever copies exist elsewhere.
    inbox = {"folder": "INBOX", "uidvalidity": "1", "uid": "7"}
    assert row_identity({**inbox, "memberships": memberships}, real) == inbox
    # Real folders are the LIST-vetted set: an INBOX that LIST marks as a view
    # is no canonical source.
    assert row_identity(inbox, {"Archive"}) == {**unknown, "alias": inbox}


def test_canonical_lookup_is_read_only_and_reads_only_database_metadata(mailbox):
    sim, client = mailbox
    target = seed(sim, labels={"Labels/Review"})
    client.read_thread(target.message_id())
    commands = [command for _, _, _, command in sim.commands]
    lookup = commands[max(i for i, command in enumerate(commands) if "HEADER" in command) + 1 :]
    assert lookup
    for command in lookup:
        assert re.fullmatch(
            r'EXAMINE "[^"]+"|UID SEARCH CHARSET UTF-8 ON 17-Oct-2026|'
            r"UID FETCH [0-9,]+ \(UID INTERNALDATE RFC822\.SIZE ENVELOPE\)|"
            rf"UID FETCH [0-9]+ \(BODY\.PEEK\[1\]<0\.{MAX_THREAD_BODY_BYTES}>\)|LOGOUT",
            command,
        ), command
    examined = [command for command in lookup if command.startswith("EXAMINE")]
    assert not any("Labels/" in command or "Starred" in command for command in examined)
    assert all(readonly for _, _, readonly, command in sim.commands if command.startswith("UID"))
    assert mutations(sim) == []


def test_uidvalidity_is_part_of_the_canonical_handle(mailbox):
    sim, client = mailbox
    target = seed(sim)
    preview = client.preview_action(
        "move_to_folder", ids=[target.message_id()], params={"folder": "Folders/Reviewed"}
    )
    assert preview["token"]
    previous = triple(preview["samples"][0])
    sim.reset_uidvalidity("INBOX")
    current = handle(sim, "INBOX", target)
    assert current[1] != previous[1]
    [read] = client.read_thread(target.message_id())["messages"]
    [searched] = client.search_mail(folder="INBOX", include_snippets=False)["messages"]
    assert triple(read) == triple(searched) == current
    # The token stays bound to the previewed (folder, UIDVALIDITY, UID) set.
    with pytest.raises(MailValidationError, match="UIDVALIDITY|changed"):
        client.move_to_folder(preview["token"])
    assert mutations(sim) == []
    assert target.folder == "INBOX"


def test_thread_deadline_during_lookup_keeps_body_and_null_canonical_fields(settings, monkeypatch):
    root = message("<root@example.com>", sections={"1": b"Requested body"})
    fake = FakeImap(
        {"INBOX": {"7": root}, "All Mail": {"70": root}}, folder_flags={"All Mail": r"\All"}
    )
    mailbox = fake_client(settings, fake)
    deadline = mailbox._deadline()
    monkeypatch.setattr(mailbox, "_deadline", lambda: deadline)
    original_uid = fake.uid

    def expire_on_lookup(command, *args):
        if command == "SEARCH" and "ON" in args:
            deadline.end = 0
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", expire_on_lookup)
    found = mailbox.read_thread("<root@example.com>")
    assert found["partial"] is False and "partial_reason" not in found
    [item] = found["messages"]
    assert item["text"] == "Requested body"
    assert triple(item) == (None, None, None)
    assert item["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "70"}


def test_search_lookup_error_keeps_the_view_page_retryable(settings):
    root = message("<root@example.com>")
    fake = FakeImap(
        {"INBOX": {"7": root}, "All Mail": {"70": root}}, folder_flags={"All Mail": r"\All"}
    )
    select = fake.select

    def refuse_inbox(folder, *, readonly):
        if folder == '"INBOX"':
            return "NO", [b"synthetic refusal"]
        return select(folder, readonly=readonly)

    fake.select = refuse_inbox
    found = fake_client(settings, fake).search_mail(folder="All Mail", since="2026-10-01")
    assert found["total"] == 1
    unresolved_search(found, lookup_failed=True)


def test_lookup_day_comes_from_the_reported_internaldate(settings):
    # 23:30 at -0500 is the next UTC day. ON compares the reported INTERNALDATE
    # day; gluon reports UTC, so both use the same day as the server's index.
    stamp = "14-Oct-2026 23:30:00 -0500"
    root = message("<late@example.com>", internaldate=stamp)
    fake = FakeImap({"INBOX": {"7": root}, "All Mail": {"70": root}}, uidnext=True)
    [item] = fake_client(settings, fake).read_thread("<late@example.com>")["messages"]
    assert triple(item) == ("INBOX", "1", "7")
    lookups = [call for call in fake.calls if call[0] == "SEARCH" and "ON" in call]
    assert lookups and all(call[3:] == ("ON", "14-Oct-2026") for call in lookups)


def test_real_folder_search_needs_no_canonical_lookup(settings):
    fake = FakeImap(
        {"INBOX": {"7": message("<root@example.com>")}, "All Mail": {}},
        folder_flags={"All Mail": r"\All"},
    )
    found = fake_client(settings, fake).search_mail(include_snippets=False)
    assert [triple(item) for item in found["messages"]] == [("INBOX", "1", "7")]
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"INBOX"']
    assert not any(call[0] == "SEARCH" and "ON" in call for call in fake.calls)


# _metadata drops a record without size, envelope or internal date; a NIL
# envelope is no envelope. None of them may count as "not another copy".
DAMAGE = {
    "omitted record": lambda record: None,
    "missing RFC822.SIZE": lambda record: re.sub(rb" RFC822\.SIZE [0-9]+", b"", record),
    "missing INTERNALDATE": lambda record: re.sub(rb' INTERNALDATE "[^"]*"', b"", record),
    "missing ENVELOPE": lambda record: record[: record.index(b" ENVELOPE ")] + b")",
    "NIL ENVELOPE": lambda record: record[: record.index(b" ENVELOPE ")] + b" ENVELOPE NIL)",
}
# Mailbox UIDs that all hold identical messages, the searched view handle, the
# damaged lookup record and the canonical handle a complete lookup reports.
LOOKUPS = {
    "label source": (
        {"INBOX": ["7"], "All Mail": ["70"], "Labels/Keep": ["10"]},
        ("Labels/Keep", "10"),
        ("Labels/Keep", "10"),
        ("INBOX", "1", "7"),
    ),
    "All Mail source": (
        {"INBOX": ["7"], "All Mail": ["70"]},
        ("All Mail", "70"),
        ("All Mail", "70"),
        ("INBOX", "1", "7"),
    ),
    # An orphan's label: one All Mail copy is that orphan, the other the INBOX
    # message. Without either All Mail record, INBOX 7 would look unique.
    "All Mail candidate": (
        {"INBOX": ["7"], "All Mail": ["70", "71"], "Labels/Keep": ["10"]},
        ("Labels/Keep", "10"),
        ("All Mail", "70"),
        None,
    ),
    # Identical deliveries into INBOX and Archive, one of them labeled: either
    # real copy could be the label's message, with or without All Mail.
    # Without All Mail no lookup runs at all; see the tests above.
    "real candidate": (
        {"INBOX": ["7"], "Archive": ["9"], "All Mail": ["70", "71"], "Labels/Keep": ["10"]},
        ("Labels/Keep", "10"),
        ("Archive", "9"),
        None,
    ),
}


def damage_lookup(fake, folder, uid, damage):
    """Return one UID's canonical lookup record from one folder incomplete."""
    original = fake.uid

    def uid_command(command, *args):
        status, data = original(command, *args)
        if command != "FETCH" or args[1] != LOOKUP_FETCH or fake.current != folder:
            return status, data
        records = [
            DAMAGE[damage](record) if f"(UID {uid} ".encode() in record else record
            for record in data
        ]
        return status, [record for record in records if record is not None]

    fake.uid = uid_command


@pytest.mark.parametrize("damage", [None, *DAMAGE])
@pytest.mark.parametrize("scenario", LOOKUPS)
def test_incomplete_lookup_metadata_never_establishes_uniqueness(settings, scenario, damage):
    mailboxes, (view, view_uid), (folder, uid), complete = LOOKUPS[scenario]
    twin = message(TWIN)
    fake = FakeImap(
        {name: dict.fromkeys(uids, twin) for name, uids in mailboxes.items()}, uidnext=True
    )
    if damage is not None:
        damage_lookup(fake, folder, uid, damage)
    found = fake_client(settings, fake).search_mail(folder=view, since="2026-10-01")
    # Matching totals survive presentation failures, but an unresolved view
    # handle cannot appear as a message that lacks its canonical handle.
    assert found["total"] == 1
    if damage is None and complete is not None:
        [hit] = found["messages"]
        assert hit["snippet"] == "Hello plain text"
        assert hit["alias"] == {"folder": view, "uidvalidity": "1", "uid": view_uid}
        assert triple(hit) == complete
        assert found["partial"] is False
    else:
        unresolved_search(found, lookup_failed=damage is not None)


@pytest.mark.parametrize("damage", [None, *DAMAGE])
def test_orphan_thread_never_takes_its_twins_handle_from_incomplete_metadata(settings, damage):
    # All Mail 70 is an orphan; All Mail 71 is another message, also INBOX 7.
    # Envelope, INTERNALDATE and size match; equal-length bodies differ.
    orphan = message(TWIN, sections={"1": b"AAA"})
    twin = message(TWIN, sections={"1": b"BBB"})
    fake = FakeImap({"INBOX": {"7": twin}, "All Mail": {"70": orphan, "71": twin}}, uidnext=True)
    if damage is not None:
        # Without UID 71's record, INBOX 7 would look like the orphan's copy.
        damage_lookup(fake, "All Mail", "71", damage)
    thread = fake_client(settings, fake).read_thread(TWIN)
    [read] = thread["messages"]
    assert read["text"] == "AAA"
    assert triple(read) == (None, None, None)
    assert read["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "70"}
    assert thread["partial"] is False


def test_thread_lookup_failure_keeps_relatives_of_a_missing_message(settings):
    reply = message(
        "<reply@example.invalid>",
        references="<gone@example.invalid>",
        sections={"1": b"Reply body"},
    )
    fake = FakeImap({"INBOX": {"8": reply}, "All Mail": {"80": reply}})
    damage_lookup(fake, "All Mail", "80", "omitted record")
    thread = fake_client(settings, fake).read_thread("<gone@example.invalid>")
    assert thread["partial"] is False
    [read] = thread["messages"]
    assert read["text"] == "Reply body"
    assert triple(read) == (None, None, None)
    assert read["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "80"}


VIEW_FLAGS = [r"\Flagged", r"\All", r"\Noselect", r"\fLaGgEd", r"\aLL", r"\NOSELECT"]


def flagged_inbox(tmp_path, flag, *, uidnext=False):
    settings = Settings(
        token="offline-test-token-at-least-thirty-two-characters",
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    service, fake, _ = cleanup_setup(settings)
    item = fake.folders["INBOX"]["1"]
    fake.folders["All Mail"]["3"] = item
    fake.folders["Labels/News"]["2"] = item
    if flag is not None:
        fake.folder_flags["INBOX"] = flag
    fake.reports_uidnext = uidnext
    return service


@pytest.mark.parametrize("flag", [None, *VIEW_FLAGS])
def test_label_samples_and_search_take_real_folders_from_list(tmp_path, flag):
    service = flagged_inbox(tmp_path, flag, uidnext=True)
    preview = service.preview_action(
        "remove_label", ids=["<1@example.test>"], params={"label": "News"}
    )
    found = service.client.search_mail(
        folder="Labels/News", since="2026-01-01", include_snippets=False
    )
    [sample] = preview["samples"]
    expected = ("INBOX", "1", "1") if flag is None else (None, None, None)
    assert triple(sample) == expected
    label = {"folder": "Labels/News", "uidvalidity": "1", "uid": "2"}
    assert sample["alias"] == label
    if flag is None:
        [hit] = found["messages"]
        assert triple(hit) == expected and hit["alias"] == label
    else:
        unresolved_search(found)
    # The approval still binds the label handle and every discovered membership.
    [grant] = list(service.tokens._grants.values())
    [row] = grant.messages
    assert triple(row) == ("Labels/News", "1", "2")
    listed = flag is None or flag.casefold() != r"\noselect"
    assert (("INBOX", "1", "1") in {triple(member) for member in row["memberships"]}) is listed


@pytest.mark.parametrize("flag", [r"\Flagged", r"\fLaGgEd", r"\All"])
def test_source_samples_and_search_agree_on_an_inbox_listed_as_a_view(tmp_path, flag):
    service = flagged_inbox(tmp_path, flag)
    preview = service.preview_action(
        "add_label", query={"folder": "INBOX"}, params={"label": "Other"}
    )
    found = service.client.search_mail(folder="INBOX", since="2026-01-01", include_snippets=False)
    [sample] = preview["samples"]
    assert triple(sample) == (None, None, None)
    inbox = {"folder": "INBOX", "uidvalidity": "1", "uid": "1"}
    assert sample["alias"] == inbox
    unresolved_search(found)


def test_execute_refusal_names_the_bound_label_source(mailbox, monkeypatch):
    sim, client = mailbox
    orphan = sim.add(Msg(hdr("<orphan@example.invalid>"), folder=None, labels={"Labels/Keep"}))
    preview = client.preview_action(
        "remove_label", ids=[orphan.message_id()], params={"label": "Keep"}
    )
    [sample] = preview["samples"]
    assert preview["token"] and triple(sample) == (None, None, None)
    # A sender protected after preview blocks execution. The refusal names the
    # bound label source, not the sample's null canonical folder.
    monkeypatch.setattr(client.cleanup.policy, "check", lambda row: ["Protected sender"])
    with pytest.raises(MailValidationError, match='during execute in "Labels/Keep"'):
        client.remove_label(preview["token"])
    assert mutations(sim) == []


@pytest.mark.parametrize(
    "listed,requested",
    [("INBOX", "inbox"), ("INBOX", "Inbox"), ("Inbox", "INBOX"), ("inbox", "INBOX")],
)
def test_inbox_in_any_ascii_case_keeps_its_real_folder_handle(
    settings, monkeypatch, listed, requested
):
    twin = message(TWIN)
    # Identical copies elsewhere would leave a canonical lookup ambiguous.
    fake = FakeImap(
        {listed: {"7": twin}, "Archive": {"9": twin}, "All Mail": {"70": twin, "71": twin}}
    )
    select = fake.select
    examined = []

    def any_case_inbox(folder, *, readonly):
        # Servers accept INBOX in any case; this double stores only LIST's name.
        examined.append(folder)
        return select(f'"{listed}"' if folder.upper() == '"INBOX"' else folder, readonly=readonly)

    def no_lookup(*args, **kwargs):
        pytest.fail("A real-folder search needs no canonical lookup")

    fake.select = any_case_inbox
    monkeypatch.setattr(canonical, "resolve_views", no_lookup)
    found = fake_client(settings, fake).search_mail(folder=requested, include_snippets=False)
    [hit] = found["messages"]
    assert triple(hit) == (listed, "1", "7") and "alias" not in hit
    assert found["partial"] is False
    # The search selects and reports the mailbox under its LIST name.
    assert examined == [f'"{listed}"']


def test_only_ascii_inbox_ignores_case_in_list_names():
    attributes = {"Inbox": set(), "Archive": set(), "Folders/Work": set()}
    assert listed_name(attributes, "INBOX") == listed_name(attributes, "inbox") == "Inbox"
    # Every other name, and a non-ASCII INBOX lookalike, must match exactly.
    for name in ("archive", "folders/Work", "ınbox"):
        assert listed_name(attributes, name) == name
