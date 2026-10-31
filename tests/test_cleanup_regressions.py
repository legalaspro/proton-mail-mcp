"""Regression ports of review probes U5a-U5d/U11, using real IMAP framing offline."""

import json
import re
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import FOLDER_ATTRS, BridgeSim, Msg, hdr, make_client, mutations

from proton_mail_mcp.cleanup_imap import CleanupMutator
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy


@pytest.fixture
def bridge(tmp_path):
    simulations = []

    def build(**options):
        sim = BridgeSim(**options)
        simulations.append(sim)
        return sim, make_client(sim, tmp_path / str(len(simulations)))

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors
        assert not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def act(client, action="move_to_trash", source="INBOX"):
    params = {"label": "Review"} if action in {"add_label", "remove_label"} else {}
    preview = client.preview_action(action, query={"folder": source}, params=params)
    assert preview["token"], preview
    completed = getattr(client, action)(preview["token"])
    assert completed["count"] == 1 and not completed["partial"], completed
    return completed


def act_unverified(sim, client, message):
    """Trash message without COPYUID while its new copy is hidden from verification.

    Leaving INBOX does not prove the move, so it stays uncertain without a
    destination UID: undo must then rely on the logged digest, size and date.
    """
    preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["token"], preview
    sim.fetch_omit.add(("Trash", message.pid))
    try:
        completed = client.move_to_trash(preview["token"])
    finally:
        sim.fetch_omit.discard(("Trash", message.pid))
    assert completed["count"] == 0 and completed["uncertain"] == 1, completed
    assert message.folder == "Trash" and completed["messages"][0]["uid_after"] is None
    return completed


def assert_missing(result, sim, before):
    assert result["count"] == 0, result
    assert result["partial"] is True, result
    assert len(result["missing"]) == 1 and result["missing"][0]["reason"], result
    assert mutations(sim) == before


def assert_preview_refused(client, source):
    try:
        preview = client.preview_action("move_to_trash", query={"folder": source})
    except MailValidationError:
        return
    assert preview["token"] is None, preview


def append_old_record(client, sim, message, *, source="INBOX", bound=False):
    record = {
        "log_id": "a" * 32,
        "action": "move_to_trash",
        "params": {},
        "partial": False,
        "messages": [
            {
                "message_id": message.message_id(),
                "source_folder": source,
                "destination_folder": "Trash",
                "folders_before": [source],
                "status": "done",
            }
        ],
    }
    if bound:
        record["messages"][0].update(
            uid_after=str(sim.uid_of("Trash", message.pid)),
            uidvalidity_after=str(sim.uidvalidity["Trash"]),
        )
    path = Path(client.settings.action_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    return record["log_id"]


def test_u5a_undo_does_not_move_collision_after_original_trashed_and_deleted(bridge):
    sim, client = bridge()
    original = sim.add(Msg(hdr("<duplicate@shop.example>", subject="Weekly deals")))
    completed = act(client)
    del sim.messages[original.pid]
    collision = sim.add(
        Msg(
            hdr(
                "<duplicate@shop.example>",
                from_="Alice <alice@corp.example>",
                subject="Signed contract - keep",
            ),
            folder="Trash",
        )
    )
    assert str(sim.uid_of("Trash", collision.pid)) != completed["messages"][0]["uid_after"]
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert collision.folder == "Trash"
    assert_missing(result, sim, before)


def test_u5b_undo_does_not_move_collision_with_all_mail_hidden(bridge):
    sim, client = bridge(show_all_mail=False)
    original = sim.add(Msg(hdr("<duplicate@shop.example>", subject="Weekly deals")))
    completed = act(client)
    original.folder = "Folders/Keep"
    collision = sim.add(Msg(hdr("<duplicate@shop.example>", subject="Different"), folder="Trash"))
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert collision.folder == "Trash" and original.folder == "Folders/Keep"
    assert_missing(result, sim, before)


def test_u5c_undo_does_not_remove_label_from_collision(bridge):
    sim, client = bridge()
    original = sim.add(Msg(hdr("<duplicate@shop.example>")))
    completed = act(client, "add_label")
    del sim.messages[original.pid]
    collision = sim.add(
        Msg(hdr("<duplicate@shop.example>", subject="Different"), labels={"Labels/Review"})
    )
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert "Labels/Review" in collision.labels
    assert_missing(result, sim, before)


def test_u5d_undo_does_not_add_label_to_all_mail_collision(bridge):
    sim, client = bridge()
    original = sim.add(Msg(hdr("<duplicate@shop.example>"), labels={"Labels/Review"}))
    completed = act(client, "remove_label", "Labels/Review")
    del sim.messages[original.pid]
    collision = sim.add(Msg(hdr("<duplicate@shop.example>", subject="Different")))
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert "Labels/Review" not in collision.labels
    assert_missing(result, sim, before)


@pytest.mark.parametrize("action", ["move_to_trash", "add_label", "remove_label"])
@pytest.mark.parametrize("failure", ["uidvalidity", "missing_uid", "message_id"])
def test_bound_undo_identity_mismatch_is_missing_without_mutation(bridge, action, failure):
    sim, client = bridge()
    original = sim.add(
        Msg(
            hdr("<verified@shop.example>"),
            labels={"Labels/Review"} if action == "remove_label" else (),
        )
    )
    source = "Labels/Review" if action == "remove_label" else "INBOX"
    completed = act(client, action, source)
    destination = completed["messages"][0]["destination_folder"]
    if failure == "uidvalidity":
        sim.reset_uidvalidity(destination)
    elif failure == "missing_uid":
        del sim.messages[original.pid]
        sim.sync()
    elif failure == "message_id":
        original.headers = hdr("<replacement@shop.example>")
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert_missing(result, sim, before)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("uidvalidity", "UIDVALIDITY changed before mutation"),
        ("message_id", "Message changed before mutation"),
    ],
)
def test_bound_undo_writable_selection_race_reports_missing(bridge, failure, reason):
    sim, client = bridge()
    message = sim.add(Msg(hdr("<late-race@shop.example>")))
    completed = act(client)
    logged_uid = int(completed["messages"][0]["uid_after"])
    replacement = None

    def race(sid, selected, operation, command):
        nonlocal replacement
        if operation == "SELECT" and command == 'SELECT "Trash"':
            with sim.lock:
                del sim.messages[message.pid]
                headers = (
                    message.headers
                    if failure == "uidvalidity"
                    else hdr("<replacement@shop.example>")
                )
                replacement = sim.add(Msg(headers, folder="Trash"))
                sim.uidmap["Trash"] = {logged_uid: replacement.pid}
                sim.next_uid["Trash"] = logged_uid + 1
                if failure == "uidvalidity":
                    sim.uidvalidity["Trash"] += 1
        return None

    sim.hooks.append(race)
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert replacement is not None
    assert replacement.folder == "Trash"
    assert_missing(result, sim, before)
    assert result["missing"][0]["reason"] == reason + ' during undo in "Trash"'


@pytest.mark.parametrize("missing_field", ["uid_after", "uidvalidity_after"])
def test_half_logged_undo_identity_never_searches_or_moves_collision(bridge, missing_field):
    sim, client = bridge()
    original = sim.add(Msg(hdr("<half-logged@shop.example>")))
    act(client)
    log_path = Path(client.settings.action_log_path)
    record = replay_records([json.loads(line) for line in log_path.read_text().splitlines()])[0]
    record["log_id"] = "b" * 32
    entry = record["messages"][0]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["header_digest"])
    del entry[missing_field]
    with log_path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    del sim.messages[original.pid]
    collision = sim.add(Msg(original.headers, folder="Trash"))
    start = len(sim.commands)
    before = mutations(sim)
    result = client.undo_action(record["log_id"])
    assert not [command for *_, command in sim.commands[start:] if "SEARCH" in command]
    assert collision.folder == "Trash"
    assert_missing(result, sim, before)
    assert result["missing"][0]["reason"] == 'invalid logged UID identity during undo in "Trash"'


@pytest.mark.parametrize("action", ["move_to_trash", "add_label", "remove_label"])
def test_bound_undo_never_searches_message_id(bridge, action):
    sim, client = bridge()
    message = sim.add(
        Msg(
            hdr("<verified@shop.example>"),
            labels={"Labels/Review"} if action == "remove_label" else (),
        )
    )
    original = sim.snapshot()
    completed = act(client, action, "Labels/Review" if action == "remove_label" else "INBOX")
    assert completed["messages"][0]["uid_after"]
    start = len(sim.commands)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]
    assert sim.snapshot() == original
    assert message.pid in sim.messages
    assert not [
        command
        for *_, command in sim.commands[start:]
        if re.search(r"\bSEARCH\b.*\bMessage-ID\b", command, re.IGNORECASE)
    ]


def test_bound_undo_restores_lost_label_without_message_id_search(bridge):
    sim, client = bridge()
    message = sim.add(Msg(hdr("<restore-label@shop.example>"), labels={"Labels/Review"}))
    original = sim.snapshot()
    completed = act(client)
    message.labels.clear()
    sim.sync()
    start = len(sim.commands)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]
    assert sim.snapshot() == original
    assert not [
        command
        for *_, command in sim.commands[start:]
        if re.search(r"\bSEARCH\b.*\bMessage-ID\b", command, re.IGNORECASE)
    ]


def test_old_record_without_digest_or_copyuid_fails_closed(bridge):
    sim, client = bridge()
    message = sim.add(Msg(hdr("<legacy@shop.example>"), folder="Trash"))
    log_id = append_old_record(client, sim, message)
    before = mutations(sim)
    result = client.undo_action(log_id)
    assert message.folder == "Trash"
    assert_missing(result, sim, before)


def test_no_copyuid_undo_uses_stored_digest_and_restores_unique_match(bridge):
    sim, client = bridge(copyuid=False)
    message = sim.add(Msg(hdr("<fallback@shop.example>")))
    completed = act_unverified(sim, client, message)
    entry = completed["messages"][0]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["header_digest"])
    start = len(sim.commands)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]
    assert message.folder == "INBOX"
    assert any(
        command.startswith("UID FETCH ") and "ENVELOPE" in command
        for *_, command in sim.commands[start:]
    )
    assert not any(
        "SEARCH" in command and "HEADER" in command for *_, command in sim.commands[start:]
    )


@pytest.mark.parametrize("changed", ["from_", "subject", "date", "ambiguous"])
def test_no_copyuid_fallback_refuses_changed_digest_or_multiple_matches(bridge, changed):
    sim, client = bridge(copyuid=False)
    message = sim.add(Msg(hdr("<fallback@shop.example>")))
    completed = act_unverified(sim, client, message)
    if changed == "ambiguous":
        sim.add(Msg(message.headers, folder="Trash"))
    else:
        replacements = {
            "from_": "Another <other@corp.example>",
            "subject": "Changed",
            "date": "Thu, 15 Oct 2026 10:00:00 +0000",
        }
        message.headers = hdr("<fallback@shop.example>", **{changed: replacements[changed]})
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert message.folder == "Trash"
    assert_missing(result, sim, before)


def test_no_copyuid_fallback_refuses_incomplete_candidate_fetch(bridge):
    sim, client = bridge(copyuid=False, show_all_mail=False)
    message = sim.add(Msg(hdr("<incomplete@shop.example>")))
    completed = act_unverified(sim, client, message)
    collision = sim.add(Msg(message.headers, folder="Trash"))
    sim.fetch_omit.add(("Trash", collision.pid))
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert message.folder == collision.folder == "Trash"
    assert_missing(result, sim, before)
    assert result["missing"] == [
        {
            "message_id": message.message_id(),
            "folder": "Trash",
            "reason": 'incomplete message fetch during undo in "Trash"; manual handling required',
        }
    ]


@pytest.mark.parametrize("source", ["Starred", "Scheduled"])
def test_u11_system_label_views_cannot_issue_move_preview(bridge, source):
    sim, client = bridge(folders=("Scheduled", "Folders/Keep"))
    sim.add(Msg(hdr("<view@shop.example>"), starred=True, labels={"Scheduled"}))
    before = sim.snapshot()
    assert_preview_refused(client, source)
    assert sim.snapshot() == before and not mutations(sim)


@pytest.mark.parametrize("attribute", [r"\Flagged", r"\All", r"\Noselect", r"\fLaGgEd"])
def test_move_source_list_attributes_are_refused(bridge, monkeypatch, attribute):
    monkeypatch.setitem(FOLDER_ATTRS, "INBOX", attribute)
    sim, client = bridge()
    sim.add(Msg(hdr("<view@shop.example>")))
    assert_preview_refused(client, "INBOX")
    assert not mutations(sim)


def test_id_resolution_uses_real_folder_for_starred_message(bridge):
    sim, client = bridge()
    sim.add(Msg(hdr("<starred@shop.example>"), starred=True))
    preview = client.preview_action("move_to_trash", ids=["<starred@shop.example>"])
    assert preview["token"] and preview["count"] == 1
    completed = client.move_to_trash(preview["token"])
    assert completed["messages"][0]["source_folder"] == "INBOX"
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]


@pytest.mark.parametrize("source", ["Starred", "Scheduled"])
def test_preflight_refuses_system_label_view_independently(bridge, source):
    sim, client = bridge(folders=("Scheduled", "Folders/Keep"))
    message = sim.add(Msg(hdr("<view@shop.example>"), starred=True, labels={"Scheduled"}))
    mutator = CleanupMutator(client, client.settings, ProtectedPolicy())
    with client._session() as (connection, _):
        rows = mutator._fetch(
            connection, client._deadline(), source, [str(sim.uid_of(source, message.pid))]
        )
        with pytest.raises(MailValidationError):
            mutator._preflight(connection, client._deadline(), "move_to_trash", "Trash", rows)
    assert not mutations(sim)


@pytest.mark.parametrize("attribute", [r"\Flagged", r"\All", r"\Noselect"])
def test_preflight_refuses_view_attribute_independently(bridge, monkeypatch, attribute):
    sim, client = bridge()
    message = sim.add(Msg(hdr("<view@shop.example>")))
    mutator = CleanupMutator(client, client.settings, ProtectedPolicy())
    with client._session() as (connection, _):
        rows = mutator._fetch(
            connection, client._deadline(), "INBOX", [str(sim.uid_of("INBOX", message.pid))]
        )
        monkeypatch.setitem(FOLDER_ATTRS, "INBOX", attribute)
        with pytest.raises(MailValidationError):
            mutator._preflight(connection, client._deadline(), "move_to_trash", "Trash", rows)
    assert not mutations(sim)


@pytest.mark.parametrize(
    "source",
    ["Starred", "Scheduled", " INBOX", "folders/Keep", "Folders/", "Folders/../X", "Fоlders/Keep"],
)
def test_legacy_undo_refuses_non_real_restore_source(bridge, source):
    sim, client = bridge(folders=(source, "Folders/Keep"))
    message = sim.add(Msg(hdr("<legacy@shop.example>"), folder="Trash"))
    log_id = append_old_record(client, sim, message, source=source, bound=True)
    before = mutations(sim)
    result = client.undo_action(log_id)
    assert message.folder == "Trash"
    assert_missing(result, sim, before)


@pytest.mark.parametrize("name", ["Inbox", "inbox"])
def test_legacy_undo_restores_the_actual_inbox_case(bridge, monkeypatch, name):
    sim, client = bridge()
    original_names, original_is_folder = sim.mailbox_names, sim.is_folder
    monkeypatch.setattr(
        sim,
        "mailbox_names",
        lambda: [name if folder == "INBOX" else folder for folder in original_names()],
    )
    monkeypatch.setattr(
        sim, "is_folder", lambda folder: folder == name or original_is_folder(folder)
    )
    message = sim.add(Msg(hdr("<legacy-inbox@shop.example>"), folder="Trash"))
    log_id = append_old_record(client, sim, message, source=name, bound=True)
    result = client.undo_action(log_id)
    assert result["count"] == 1 and not result["partial"]
    assert message.folder == name


@pytest.mark.parametrize("attribute", [r"\Flagged", r"\All", r"\Noselect"])
def test_legacy_undo_refuses_restore_source_with_view_attribute(bridge, monkeypatch, attribute):
    sim, client = bridge()
    message = sim.add(Msg(hdr("<legacy-attribute@shop.example>"), folder="Trash"))
    log_id = append_old_record(client, sim, message, bound=True)
    monkeypatch.setitem(FOLDER_ATTRS, "INBOX", attribute)
    before = mutations(sim)
    result = client.undo_action(log_id)
    assert message.folder == "Trash"
    assert_missing(result, sim, before)


@pytest.mark.parametrize(
    "source",
    [
        " INBOX",
        "folders/Keep",
        "Folders/",
        "Folders/../X",
        'Folders/quote"',
        "Folders/line\r\nbreak",
        "Fоlders/Keep",
    ],
)
def test_move_preview_refuses_folder_name_tricks(bridge, source):
    sim, client = bridge(folders=(source,))
    sim.add(Msg(hdr("<trick@shop.example>"), folder=source))
    assert_preview_refused(client, source)
    assert not mutations(sim)
