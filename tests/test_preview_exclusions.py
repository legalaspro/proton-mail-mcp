"""Preview isolates unsafe identities while every approval remains exact and strict."""

import json
from dataclasses import replace

import pytest
from action_records import execution_log
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from certified_index_peer import CondstoreMixin

from proton_mail_mcp.cleanup_response import cleanup_response
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.guarded import _fingerprint
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy

SENDER = "notifications@github.com"
DESTINATION = "Folders/Reviewed"
QUERY = {"folder": "INBOX", "from_": SENDER, "before": "2026-10-01"}
PARAMS = {"folder": DESTINATION, "mark_read": True}


class SeptemberMailbox(BridgeSim):
    """Keep the socket protocol real while dating synthetic arrivals in September."""

    def handle(self, *args, **kwargs):
        response = super().handle(*args, **kwargs)
        return response.replace(b'INTERNALDATE "17-Oct-2026', b'INTERNALDATE "20-Sep-2026')


class IndexedSeptemberMailbox(CondstoreMixin, SeptemberMailbox):
    """The same offline mailbox with certification for hidden raw-ID discovery."""


@pytest.fixture
def github_mailbox(tmp_path):
    simulations = []

    def build(count=36, *, bad_count=1, variant="display", indexed=False, **options):
        sim = (IndexedSeptemberMailbox if indexed else SeptemberMailbox)(**options)
        simulations.append(sim)
        client = make_client(sim, tmp_path / str(len(simulations)), call_timeout=300)
        if indexed:
            client.settings = replace(
                client.settings,
                identity_index_enabled=True,
                identity_index_dir=str(tmp_path / str(len(simulations)) / "index"),
            )
        sources = [
            sim.add(
                Msg(
                    hdr(
                        f"<example-owner/example-repo/pull/{index}/before/aaa/after/bbb@github.com>",
                        from_=SENDER,
                        date="Sun, 20 Sep 2026 09:00:00 +0000",
                    )
                )
            )
            for index in range(1, count + 1)
        ]
        copies = []
        for source in sources[:bad_count]:
            sender = "GitHub <notifications@github.com>" if variant == "display" else SENDER.upper()
            copies.append(
                sim.add(Msg(source.headers.replace(SENDER.encode(), sender.encode()), folder=None))
            )
        return sim, client, sources, copies

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def preview(client, **kwargs):
    return client.preview_action("move_to_folder", query=QUERY, params=PARAMS, **kwargs)


def assert_excluded(result, identifiers, reason):
    assert result["excluded_count"] == len(identifiers)
    assert result["excluded_ids"] == identifiers[:20]
    assert result["excluded_truncated"] is (len(identifiers) > 20)
    assert result["excluded_reasons"] == {reason: len(identifiers)}


@pytest.mark.parametrize("variant", ["display", "case"])
def test_github_duplicate_is_excluded_and_only_clean_limit_executes(github_mailbox, variant):
    sim, client, sources, copies = github_mailbox(variant=variant)
    result = preview(client, limit=30)
    bad_id = sources[0].message_id()
    assert_excluded(result, [bad_id], "inconsistent_sender_headers")
    assert result["count"] == 30 and result["token"] and not result["blocked"]
    # G-B: one batched chunk confirms the 30-message batch in one call.
    assert result["estimated_calls"] == 1
    assert not mutations(sim)
    grant = client.cleanup.tokens.inspect(result["token"], "move_to_folder")
    expected = [item.message_id() for item in sources[1:31]]
    assert [row["message_id"] for row in grant.messages] == expected
    assert bad_id not in {row["message_id"] for row in grant.messages}
    assert grant.fingerprint == _fingerprint(grant.messages)
    assert grant.bounded_recovery and all("label_states" in row for row in grant.messages)

    completed = client.move_to_folder(result["token"])
    assert completed["done"] == completed["run_done"] == 30
    assert completed["remaining"] == 0 and not completed["partial"]
    assert [row["message_id"] for row in completed["messages"]] == expected
    assert all(item.folder == DESTINATION and item.flags == {r"\Seen"} for item in sources[1:31])
    assert all(item.folder == "INBOX" and not item.flags for item in [sources[0], *sources[31:]])
    assert all(item.folder is None and not item.flags for item in copies)
    commands = mutations(sim)
    assert {command.split()[1] for *_, command in commands} == {"MOVE", "STORE"}
    compact = cleanup_response(completed)
    preview_size = len(json.dumps(result).encode())
    execute_size = len(json.dumps(compact).encode())
    assert preview_size < 10_000 and execute_size < 10_000
    print(
        f"github_candidates=36 limit=30 preview_bytes={preview_size} execute_bytes={execute_size}"
    )


def test_excluded_identity_cannot_be_widened_or_tampered_into_approval(github_mailbox):
    sim, client, sources, _ = github_mailbox()
    result = preview(client, limit=30)
    token = result["token"]
    grant = client.cleanup.tokens.inspect(token, "move_to_folder")
    clean_ids = [row["message_id"] for row in grant.messages]
    with pytest.raises(MailValidationError, match="widen"):
        client.move_to_folder(token, limit=31)
    with pytest.raises(MailValidationError, match="message set"):
        client.cleanup.execute("move_to_folder", token, ids=[*clean_ids, sources[0].message_id()])
    with pytest.raises(MailValidationError, match="token"):
        client.move_to_folder(token + "tampered")
    with pytest.raises(MailValidationError, match="Continuation exceeds"):
        client.cleanup.tokens.continue_grant(
            grant, [{**grant.messages[0], "message_id": sources[0].message_id()}]
        )
    assert not mutations(sim) and not execution_log(client.cleanup.backend.log.path)
    assert client.cleanup.tokens.inspect(token, "move_to_folder").messages == grant.messages
    assert client.move_to_folder(token)["done"] == 30
    assert sources[0].folder == "INBOX" and not sources[0].flags


def test_exclusion_report_is_capped_and_all_excluded_has_no_token(github_mailbox):
    sim, client, sources, _ = github_mailbox(25, bad_count=25)
    result = preview(client, limit=30)
    assert_excluded(result, [item.message_id() for item in sources], "inconsistent_sender_headers")
    assert result["count"] == result["estimated_calls"] == 0
    assert result["token"] is None and result["expires_at"] is None
    assert "excluded" in result["reason"].lower()
    assert not client.cleanup.tokens._grants and not mutations(sim)
    assert len(json.dumps(result).encode()) < 10_000


def test_long_excluded_ids_keep_the_default_preview_compact(github_mailbox):
    sim, client, sources, copies = github_mailbox(9, bad_count=9)
    for index, (source, copy) in enumerate(zip(sources, copies)):
        identifier = f"<synthetic-{index}-{'x' * 900}@example.invalid>"
        old = source.message_id().encode()
        source.headers = source.headers.replace(old, identifier.encode())
        copy.headers = copy.headers.replace(old, identifier.encode())
    result = preview(client)
    assert result["token"] is None and result["excluded_count"] == 9
    assert 0 < len(result["excluded_ids"]) < 9 and result["excluded_truncated"]
    assert result["excluded_reasons"] == {"inconsistent_sender_headers": 9}
    assert sum(len(json.dumps(identifier)) for identifier in result["excluded_ids"]) <= 4096
    assert len(json.dumps(result).encode()) < 10_000
    assert not mutations(sim)


def test_continuation_stays_bound_and_new_preview_rechecks_exclusions(github_mailbox):
    sim, client, sources, _ = github_mailbox()
    initial = preview(client, limit=30)
    first = client.move_to_folder(initial["token"], limit=10)
    assert first["done"] == 10 and first["remaining"] == 20
    continuation = first["continuation_token"]
    approved = client.cleanup.tokens.inspect(continuation, "move_to_folder")
    assert {row["message_id"] for row in approved.messages} == {
        item.message_id() for item in sources[11:31]
    }
    # Cleanup continuations never choose new candidates. A caller requesting a
    # fresh preview of the remaining query gets a separately checked clean set.
    refreshed = preview(client, limit=30)
    assert_excluded(refreshed, [sources[0].message_id()], "inconsistent_sender_headers")
    fresh_grant = client.cleanup.tokens.inspect(refreshed["token"], "move_to_folder")
    assert {row["message_id"] for row in fresh_grant.messages} == {
        item.message_id() for item in sources[11:]
    }
    final = client.move_to_folder(continuation)
    assert final["done"] == 20 and final["remaining"] == 0
    assert all(item.folder == "INBOX" and not item.flags for item in [sources[0], *sources[31:]])


def test_changed_approved_row_refuses_before_claim_mutation_or_journal(github_mailbox, monkeypatch):
    sim, client, sources, _ = github_mailbox(4, bad_count=0)
    token = preview(client)["token"]
    original = client.cleanup.tokens.inspect(token, "move_to_folder")
    sources[-1].headers = sources[-1].headers.replace(b"Subject: Hello", b"Subject: Changed")
    claims = []
    claim = client.cleanup.tokens.claim

    def observe_claim(*args, **kwargs):
        claims.append(args)
        return claim(*args, **kwargs)

    monkeypatch.setattr(client.cleanup.tokens, "claim", observe_claim)
    with pytest.raises(MailValidationError, match="changed"):
        client.move_to_folder(token)
    assert not claims and not mutations(sim)
    assert not execution_log(client.cleanup.backend.log.path)
    assert all(item.folder == "INBOX" and not item.flags for item in sources)
    assert client.cleanup.tokens.inspect(token, "move_to_folder").messages == original.messages


@pytest.mark.parametrize("stage", ["execute", "continuation"])
def test_folder_rules_keep_inconsistent_sender_whole_batch_strict(
    github_mailbox, tmp_path, monkeypatch, stage
):
    sim, client, sources, copies = github_mailbox(4, bad_count=0)
    path = tmp_path / "synthetic-rules.json"
    path.write_text(
        json.dumps(
            {"version": 1, "rules": [{"field": "from", "contains": SENDER, "folder": DESTINATION}]}
        )
    )
    rules = FolderRulesService(client.cleanup, path)
    token = rules.preview_rules()["token"]
    if stage == "continuation":
        first = rules.apply_rules(token, limit=1)
        assert first["done"] == 1 and first["remaining"] == 3
        token = first["continuation_token"]
    copies.append(
        sim.add(
            Msg(
                sources[-1].headers.replace(SENDER.encode(), b"GitHub <notifications@github.com>"),
                folder=None,
            )
        )
    )
    before = mutations(sim)
    log = client.cleanup.backend.log.path
    logged = log.read_bytes() if log.exists() else None
    claims = []
    claim = client.cleanup.tokens.claim

    def observe_claim(*args, **kwargs):
        claims.append(args)
        return claim(*args, **kwargs)

    monkeypatch.setattr(client.cleanup.tokens, "claim", observe_claim)
    with pytest.raises(MailValidationError, match="inconsistent sender"):
        rules.apply_rules(token)
    assert mutations(sim) == before and not claims
    assert (log.read_bytes() if log.exists() else None) == logged
    assert client.cleanup.tokens.inspect(token, "apply_rules")
    assert all(item.folder == "INBOX" for item in sources[int(stage == "continuation") :])


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing_source", "missing_source"),
        ("duplicate_source", "ambiguous_source"),
        ("duplicate_membership", "ambiguous_membership"),
        ("implicit_source", "ambiguous_membership"),
        ("label_ownership", "ambiguous_label_membership"),
        ("source_changed", "source_membership_changed"),
        ("existing_label", "label_already_present"),
        ("missing_all_mail", "missing_all_mail_copy"),
        ("invalid_source", "invalid_identity"),
        ("invalid_membership", "invalid_identity"),
        ("hidden_raw_identity", "invalid_identity"),
    ],
)
def test_item_level_preview_checks_exclude_only_affected_identity(
    github_mailbox, monkeypatch, fault, reason
):
    sim, client, sources, _ = github_mailbox(2, bad_count=0, indexed=fault == "hidden_raw_identity")
    bad, clean = sources
    bad_id = bad.message_id()
    action, params = "move_to_folder", {"folder": DESTINATION}
    selector = {"query": {"folder": "INBOX"}}
    if fault == "missing_source":
        bad.folder = None
        selector = {"ids": [bad_id, clean.message_id()]}
    elif fault == "duplicate_source":
        sim.add(Msg(bad.headers, folder="Archive"))
        selector = {"ids": [bad_id, clean.message_id()]}
    elif fault == "duplicate_membership":
        sim.add(Msg(bad.headers, folder=None))
        action, params = "mark_read", None
        selector = {"ids": [bad_id, clean.message_id()]}
    elif fault == "implicit_source":
        sim.add(Msg(bad.headers, folder="Archive"))
        selector = {"query": {}}
    elif fault == "label_ownership":
        sim.add(Msg(bad.headers))
        bad.labels.add("Labels/Review")
    elif fault == "source_changed":
        folder_rows = client.cleanup._folder_rows

        def hide_source_membership(*args, **kwargs):
            rows = folder_rows(*args, **kwargs)
            if args[2] == "INBOX" and kwargs.get("stage") == "membership":
                rows = [row for row in rows if row["message_id"] != bad_id]
            return rows

        monkeypatch.setattr(client.cleanup, "_folder_rows", hide_source_membership)
    elif fault == "existing_label":
        bad.labels.add("Labels/Review")
        action, params = "add_label", {"label": "Review"}
    elif fault == "missing_all_mail":
        for item in sources:
            item.labels.add("Labels/Review")
        action, params = "remove_label", {"label": "Review"}
        selector = {"query": {"folder": "Labels/Review"}}
        members = sim.members

        def missing_recovery_copy(name):
            return [pid for pid in members(name) if name != "All Mail" or pid != bad.pid]

        monkeypatch.setattr(sim, "members", missing_recovery_copy)
    elif fault == "invalid_source":
        bad.headers = bad.headers.replace(bad_id.encode(), (bad_id + " (relay)").encode())
        bad_id = bad.message_id()
    elif fault == "invalid_membership":
        sim.add(
            Msg(bad.headers.replace(bad_id.encode(), (bad_id + " (relay)").encode()), folder=None)
        )
    else:
        sim.add(
            Msg(
                hdr("<unrelated@example.invalid>", from_=SENDER)
                + f"Message-ID: {bad_id}\r\n".encode(),
                folder="Archive",
            )
        )
    result = client.preview_action(action, params=params, **selector)
    assert_excluded(result, [bad_id], reason)
    assert result["count"] == 1 and result["token"] and not result["blocked"]
    grant = client.cleanup.tokens.inspect(result["token"], action)
    assert [row["message_id"] for row in grant.messages] == [clean.message_id()]
    assert grant.fingerprint == _fingerprint(grant.messages)
    assert not mutations(sim)


def test_multi_identity_membership_excludes_every_implicated_id(github_mailbox):
    sim, client, sources, _ = github_mailbox(3, bad_count=0)
    bad_ids = [item.message_id() for item in sources[:2]]
    sim.add(Msg(hdr(" ".join(bad_ids), from_=SENDER), folder=None))
    result = preview(client)
    assert_excluded(result, bad_ids, "ambiguous_identity")
    assert result["count"] == 1 and result["token"] and not result["blocked"]
    grant = client.cleanup.tokens.inspect(result["token"], "move_to_folder")
    assert [row["message_id"] for row in grant.messages] == [sources[-1].message_id()]
    assert not mutations(sim)


@pytest.mark.parametrize("fault", ["sender_mismatch", "invalid_source", "invalid_membership"])
def test_exclusion_never_removes_a_protected_sender_refusal(github_mailbox, tmp_path, fault):
    sim, client, sources, _ = github_mailbox(2, bad_count=int(fault == "sender_mismatch"))
    bad = sources[0]
    policy = tmp_path / "synthetic-policy.json"
    policy.write_text(json.dumps({"addresses": ["protected@example.invalid"]}))
    client.cleanup.policy = ProtectedPolicy(str(policy))
    if fault == "invalid_source":
        bad.headers += f"Message-ID: {bad.message_id()}\r\n".encode()
    protected = bad.headers.replace(SENDER.encode(), b"protected@example.invalid")
    if fault == "invalid_membership":
        protected += f"Message-ID: {bad.message_id()}\r\n".encode()
    sim.add(Msg(protected, folder=None))
    result = preview(client)
    assert result["token"] is None and result["blocked"]
    assert any("Protected personal address" in row["reason"] for row in result["blocked"])
    assert not mutations(sim) and not client.cleanup.tokens._grants
