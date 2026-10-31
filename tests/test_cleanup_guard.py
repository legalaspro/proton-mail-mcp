"""Every cleanup capability guard is exercised without a live IMAP connection."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from resume_assertions import assert_bound_progress, identity
from resume_recording import journal_recording_mutator
from test_mail import FakeImap, client, message

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import CleanupService, TokenStore, approval_hash
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.protected import ProtectedPolicy


@pytest.fixture
def settings(tmp_path):
    return Settings(
        token="offline-test-token-at-least-thirty-two-characters",
        bridge_user="reader@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )


class RecordingMutator:
    def __init__(self):
        self.applied = []

    def validate_target(self, action, params, connection):
        if action == "move_to_folder":
            return params["folder"]
        if action in {"add_label", "remove_label"}:
            return "Labels/" + params["label"]
        return "Trash" if action == "move_to_trash" else "Archive"

    def apply(
        self,
        connection,
        deadline,
        action,
        params,
        rows,
        *,
        before_chunk=None,
        before_mutation=None,
        token_digest=None,
    ):
        if before_chunk is not None:
            before_chunk()
        if before_mutation is not None:
            before_mutation()
        self.applied.append((action, copy.deepcopy(params), copy.deepcopy(rows)))
        return {
            "count": len(rows),
            "partial": False,
            "log_id": f"offline-action-{len(self.applied)}",
            "messages": [{"message_id": row["message_id"], "status": "done"} for row in rows],
        }


def setup(settings, *, count=1, clock=None):
    fake = FakeImap(
        {
            "INBOX": {
                str(index): message(f"<{index}@example.test>") for index in range(1, count + 1)
            },
            "Archive": {},
            "Trash": {},
            "Labels/News": {},
            "All Mail": {},
        }
    )
    backend = RecordingMutator()
    tokens = TokenStore(clock=clock) if clock else TokenStore()
    service = CleanupService(client(settings, fake), backend=backend, tokens=tokens)
    return service, fake, backend


def test_preview_is_read_only_and_returns_ten_samples(settings):
    service, fake, backend = setup(settings, count=12)
    result = service.preview_action("archive", query={})
    assert result["count"] == 12
    assert len(result["samples"]) == 10
    # Samples carry the canonical (folder, UIDVALIDITY, UID) of their real source.
    assert set(result["samples"][0]) == {
        "from",
        "subject",
        "date",
        "folder",
        "uidvalidity",
        "uid",
        "id",
        "message_id",
    }
    assert result["token"] and result["expires_at"]
    assert result["action"] == "archive" and result["target"] == "Archive"
    assert result["blocked"] == []
    assert backend.applied == []
    assert {call[0] for call in fake.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "LOGOUT",
    }
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert all("ENVELOPE" in call[2] for call in fake.calls if call[0] == "FETCH")
    assert all(
        call[2] == "(UID ENVELOPE)"
        or "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]" in call[2]
        for call in fake.calls
        if call[0] == "FETCH"
    )
    assert any(
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]" in call[2]
        for call in fake.calls
        if call[0] == "FETCH"
    )


def test_successful_confirmation_then_token_reuse_refused(settings):
    service, _, backend = setup(settings)
    preview = service.preview_action("move_to_trash", ids=["<1@example.test>"])
    assert service.execute("move_to_trash", preview["token"])["count"] == 1
    with pytest.raises(MailValidationError, match="already used"):
        service.execute("move_to_trash", preview["token"])
    assert len(backend.applied) == 1


def test_token_expiry_refuses_without_opening_mail(settings):
    clock = [10.0]
    service, fake, backend = setup(settings, clock=lambda: clock[0])
    preview = service.preview_action("archive", query={})
    before = len(fake.calls)
    clock[0] += 600
    with pytest.raises(MailValidationError, match="expired"):
        service.execute("archive", preview["token"])
    assert len(fake.calls) == before
    assert backend.applied == []


@pytest.mark.parametrize(
    "mutation,error",
    [
        ({"action": "move_to_trash"}, "action"),
        ({"params": {"folder": "Folders/Other"}}, "params"),
        ({"ids": ["<different@example.test>"]}, "message set"),
    ],
)
def test_token_action_params_and_message_set_mismatch_refuse(settings, mutation, error):
    service, fake, backend = setup(settings)
    preview = service.preview_action("archive", query={})
    before = len(fake.calls)
    arguments = {"action": "archive", "token": preview["token"], **mutation}
    with pytest.raises(MailValidationError, match=error):
        service.execute(**arguments)
    assert backend.applied == []
    assert len(fake.calls) == before


@pytest.mark.parametrize("mutation", ["uid", "uidvalidity", "message_id", "membership"])
def test_changed_bound_identity_refuses_entire_batch(settings, mutation):
    service, fake, backend = setup(settings)
    token = service.preview_action("archive", query={})["token"]
    if mutation == "uid":
        fake.folders["INBOX"]["2"] = fake.folders["INBOX"].pop("1")
    elif mutation == "uidvalidity":
        fake.uidvalidity = "2"
    elif mutation == "message_id":
        fake.folders["INBOX"]["1"] = message("<different@example.test>")
    else:
        fake.folders["Labels/News"]["3"] = fake.folders["INBOX"]["1"]
    with pytest.raises(MailValidationError, match="changed"):
        service.execute("archive", token)
    assert backend.applied == []


def test_missing_message_after_preview_refuses(settings):
    service, fake, backend = setup(settings)
    token = service.preview_action("archive", ids=["<1@example.test>"])["token"]
    fake.folders["INBOX"].clear()
    with pytest.raises(MailValidationError, match="not found"):
        service.execute("archive", token)
    assert backend.applied == []


def test_cap_at_preview_returns_count_without_token(settings):
    service, _, backend = setup(settings, count=201)
    preview = service.preview_action("archive", query={})
    assert preview["count"] == 201
    assert preview["token"] is None and preview["expires_at"] is None
    assert "200" in preview["reason"]
    assert backend.applied == []


def test_query_arrivals_cannot_expand_the_bound_execution_set(settings):
    service, fake, backend = setup(settings)
    token = service.preview_action("archive", query={})["token"]
    fake.folders["INBOX"].update({str(i): message(f"<{i}@example.test>") for i in range(2, 202)})
    assert service.execute("archive", token)["count"] == 1
    assert len(backend.applied) == 1
    assert [row["uid"] for row in backend.applied[0][2]] == ["1"]


def test_cap_allows_exactly_200(settings, monkeypatch):
    settings = replace(settings, call_timeout=120, execute_timeout=120)
    service, _, backend = setup(settings, count=200)
    token = service.preview_action("archive", query={})["token"]
    assert token
    journal_recording_mutator(monkeypatch, backend, settings.action_log_path)
    grant = service.tokens.inspect(token, "archive")
    original_token, results = token, []
    while token:
        result = service.execute("archive", token)
        results.append(result)
        actual = [identity(row) for _, _, rows in backend.applied for row in rows]
        assert_bound_progress(service, grant, results, actual)
        token = result["continuation_token"]
    assert sum(result["done"] for result in results) == 200
    # One batched chunk confirms the whole 200-message approval in one call.
    assert [len(rows) for _, _, rows in backend.applied] == [200] and len(results) == 1
    assert set(actual) == {identity(row) for row in grant.messages}
    before = len(backend.applied)
    with pytest.raises(MailValidationError, match="already used"):
        service.execute("archive", original_token)
    assert len(backend.applied) == before


def test_cap_is_rechecked_on_stored_snapshot(settings):
    service, _, backend = setup(settings)
    token = service.preview_action("archive", query={})["token"]
    grant = next(iter(service.tokens._grants.values()))
    grant.messages *= 201
    # Re-bind the oversized snapshot: the cap still refuses it, independently
    # of the binding check that refuses any unbound change to a stored grant.
    grant.binding = approval_hash(grant.action, grant.params, grant.selector, grant.messages)
    with pytest.raises(MailValidationError, match="200"):
        service.execute("archive", token)
    assert backend.applied == []


def test_any_protected_message_blocks_whole_preview_and_execution(settings):
    service, fake, backend = setup(settings, count=2)
    original = fake.folders["INBOX"]["2"].headers
    fake.folders["INBOX"]["2"].headers = original.replace(
        b"sender@example.com", b"alerts@sub.bank.example.com"
    )
    preview = service.preview_action("archive", query={})
    assert preview["count"] == 2 and len(preview["blocked"]) == 1
    assert preview["blocked"][0]["id"] == "<2@example.test>"
    assert "Protected" in preview["blocked"][0]["reason"]
    assert preview["token"] is None
    fake.folders["INBOX"]["2"].headers = original
    token = service.preview_action("archive", query={})["token"]
    fake.folders["INBOX"]["2"].headers = original.replace(
        b"sender@example.com", b"person@gmail.com"
    )
    with pytest.raises(MailValidationError, match="protected"):
        service.execute("archive", token)
    assert backend.applied == []


@pytest.mark.parametrize(
    "header",
    [
        b"Reply-To: private@gmail.com\r\n",
        b"Sender: alerts@secure.bank.example.com\r\n",
        b"Reply-To: not-an-address\r\n",
        b"From: hidden@broker.example.org\r\n",
    ],
)
def test_resolver_checks_first_sender_headers(settings, header):
    service, fake, _ = setup(settings)
    item = fake.folders["INBOX"]["1"]
    item.headers = header + item.headers
    result = service.preview_action("archive", query={})
    assert result["blocked"] and result["token"] is None


def test_missing_message_id_is_blocked(settings):
    service, fake, _ = setup(settings)
    item = fake.folders["INBOX"]["1"]
    item.headers = item.headers.replace(b"Message-ID: <1@example.test>\r\n", b"")
    result = service.preview_action("archive", query={})
    assert result["token"] is None
    assert result["excluded_count"] == 1
    assert result["excluded_reasons"] == {"invalid_identity": 1}


def test_missing_sender_is_blocked(settings):
    service, fake, _ = setup(settings)
    item = fake.folders["INBOX"]["1"]
    item.headers = item.headers.replace(b"From: Sender <sender@example.com>\r\n", b"")
    result = service.preview_action("archive", query={})
    assert result["token"] is None and result["blocked"]


def test_ambiguous_id_across_sources_refuses(settings):
    service, fake, _ = setup(settings)
    fake.folders["Archive"]["2"] = fake.folders["INBOX"]["1"]
    result = service.preview_action("archive", ids=["<1@example.test>"])
    assert result["token"] is None and result["excluded_ids"] == ["<1@example.test>"]
    assert result["excluded_reasons"] == {"ambiguous_source": 1}


def test_add_label_refuses_preexisting_membership(settings):
    service, fake, _ = setup(settings)
    fake.folders["Labels/News"]["2"] = fake.folders["INBOX"]["1"]
    result = service.preview_action("add_label", query={}, params={"label": "News"})
    assert result["token"] is None and result["excluded_ids"] == ["<1@example.test>"]
    assert result["excluded_reasons"] == {"label_already_present": 1}


def test_remove_label_resolves_label_uid_and_requires_all_mail_copy(settings):
    service, fake, _ = setup(settings)
    fake.folders["Labels/News"]["2"] = fake.folders["INBOX"]["1"]
    refused = service.preview_action(
        "remove_label", ids=["<1@example.test>"], params={"label": "News"}
    )
    assert refused["token"] is None and refused["excluded_ids"] == ["<1@example.test>"]
    assert refused["excluded_reasons"] == {"missing_all_mail_copy": 1}
    fake.folders["All Mail"]["3"] = fake.folders["INBOX"]["1"]
    # UIDNEXT lets the sample's lookup prove that no mailbox changed meanwhile.
    fake.reports_uidnext = True
    result = service.preview_action(
        "remove_label", ids=["<1@example.test>"], params={"label": "News"}
    )
    assert result["token"]
    # The sample names the message's real folder; the label source is the alias.
    assert {key: result["samples"][0][key] for key in ("folder", "uidvalidity", "uid")} == {
        "folder": "INBOX",
        "uidvalidity": "1",
        "uid": "1",
    }
    assert result["samples"][0]["alias"] == {
        "folder": "Labels/News",
        "uidvalidity": "1",
        "uid": "2",
    }
    grant = next(iter(service.tokens._grants.values()))
    assert grant.messages[0]["uid"] == "2"
    assert grant.messages[0]["folders"] == ["All Mail", "INBOX", "Labels/News"]


@pytest.mark.parametrize(
    "action,kwargs",
    [
        ("archive", {}),
        ("archive", {"query": {}, "ids": ["<1@example.test>"]}),
        ("archive", {"query": {"limit": 500}}),
        ("archive", {"ids": ["two tokens"]}),
        ("archive", {"ids": ["<1@example.test>", "<1@example.test>"]}),
        ("permanent_delete", {"query": {}}),
        ("archive", {"query": {}, "params": {"folder": "Trash"}}),
        ("move_to_folder", {"query": {}, "params": {"folder": "Trash"}}),
        ("add_label", {"query": {}, "params": {"label": "../Trash"}}),
    ],
)
def test_invalid_input_rejected_before_any_connection(settings, action, kwargs):
    service, fake, backend = setup(settings)
    with pytest.raises(MailValidationError):
        service.preview_action(action, **kwargs)
    assert fake.calls == [] and backend.applied == []


def test_preview_deadline_never_issues_token(settings, monkeypatch):
    service, _, backend = setup(settings)

    def expire(*args, **kwargs):
        raise CallDeadlineExceeded("Synthetic deadline")

    monkeypatch.setattr(service, "_resolve", expire)
    result = service.preview_action("archive", query={})
    assert result["partial"] and not result["total_is_exact"]
    assert result["token"] is None and backend.applied == []


def test_token_store_random_secret_compare_digest_and_no_plaintext_storage(monkeypatch):
    generated = []
    compared = []
    monkeypatch.setattr(
        "proton_mail_mcp.guarded.secrets.token_urlsafe",
        lambda size: generated.append(size) or "x" * 43,
    )
    from proton_mail_mcp import guarded

    original = guarded.hmac.compare_digest
    monkeypatch.setattr(
        guarded.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or original(a, b)
    )
    tokens = TokenStore()
    token, _ = tokens.issue(
        "archive", {}, {"query": {}}, [{"folder": "INBOX", "uid": "1", "message_id": "<a@b>"}]
    )
    assert generated == [32]
    assert token not in repr(tokens._grants)
    tokens.claim(token, "archive")
    assert compared


def test_token_claim_is_atomic_under_concurrency():
    store = TokenStore()
    token, _ = store.issue(
        "archive", {}, {}, [{"folder": "INBOX", "uid": "1", "message_id": "<a@b>"}]
    )

    def claim():
        try:
            store.claim(token, "archive")
            return True
        except MailValidationError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: claim(), range(8))) == 1


@pytest.mark.parametrize("ttl", [0, -1, 1801, float("nan"), float("inf"), True])
def test_token_lifetime_maximum_and_invalid_values(ttl):
    with pytest.raises(MailValidationError):
        TokenStore(ttl)
    with pytest.raises(ConfigError):
        Settings(token="x" * 32, bridge_user="user@example.test", cleanup_token_ttl=ttl)


def test_cleanup_config_defaults_and_environment_paths():
    settings = Settings.from_env(
        {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "user@example.test"}
    )
    assert settings.cleanup_token_ttl == 600
    changed = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": "x" * 32,
            "PROTON_BRIDGE_USER": "user@example.test",
            "PROTON_MCP_CLEANUP_TOKEN_TTL": "1800",
            "PROTON_MCP_PROTECTED_CONFIG": "/local/rules.json",
            "PROTON_MCP_ACTION_LOG": "/local/actions.jsonl",
        }
    )
    assert changed.cleanup_token_ttl == 1800
    assert changed.protected_config_path == "/local/rules.json"
    assert changed.action_log_path == "/local/actions.jsonl"


@pytest.mark.parametrize(
    "field,address",
    [
        ("from", "Alerts <alerts@mail.bank.example.com>"),
        ("from", "alerts@secure.broker.example.org"),
        ("from", "alerts@regionalbank.example"),
        ("from", "alerts@discount-broker.example"),
        ("from", "info@service.gov"),
        ("from", "info@service.gov.uk"),
        ("from", "alerts@insurance.example"),
        ("from", "Kantonales Steueramt <info@tax.example>"),
        ("from", "Steuerverwaltung <info@office.example>"),
        ("from", "Private Insurance <info@office.example>"),
        ("sender", "private@sub.gmail.com"),
        ("reply_to", "Private <person@proton.me>"),
        ("reply_to", "Private <person@protonmail.com>"),
        ("reply_to", "Private <person@icloud.com>"),
        ("reply_to", "Private <person@outlook.com>"),
        ("reply_to", "Private <person@gmx.ch>"),
        ("reply_to", "Private <person@bluewin.ch>"),
    ],
)
def test_protected_default_domains_subdomains_headers_and_display_names(field, address):
    row = {"from": "News <info@newsletter.example>", field: address}
    assert ProtectedPolicy().check(row)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not an address",
        "broken <",
        "a@",
        "@example.com",
        "one@example.com, broken",
        "safe@example.com\nReply-To: danger@example.com",
    ],
)
def test_sender_parse_failures_are_blocked(value):
    assert ProtectedPolicy().check({"from": value})


def test_sender_missing_and_malformed_optional_headers_fail_closed():
    policy = ProtectedPolicy()
    assert policy.check({})
    assert policy.check({"from": "good@example.com", "reply_to": ""})
    assert policy.check({"from": "good@example.com", "sender": "malformed"})
    assert policy.check({"from": "good@example.com", "sender": None, "reply_to": None})
    assert policy.check({"from": "good@example.com"}) == []


def test_local_policy_extends_defaults_and_is_configurable(tmp_path):
    path = tmp_path / "local.json"
    path.write_text(
        json.dumps(
            {
                "addresses": ["relative@example.test"],
                "domains": ["family.example"],
                "protect_personal_domains": False,
            }
        )
    )
    policy = ProtectedPolicy(str(path))
    assert policy.check({"from": "relative@example.test"})
    assert policy.check({"from": "hello@sub.family.example"})
    assert policy.check({"from": "alerts@bank.example.com"})
    assert policy.check({"from": "person@gmail.com"}) == []
    path.write_text(json.dumps({"mode": "replace", "addresses": ["relative@example.test"]}))
    replaced = ProtectedPolicy(str(path))
    assert replaced.check({"from": "relative@example.test"})
    assert replaced.check({"from": "alerts@bank.example.com"}) == []


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        '{"addresses": "private@example.test"}',
        '{"mode":"typo"}',
        '{"unknown":[]}',
        '{"protect_personal_domains": "false"}',
    ],
)
def test_invalid_policy_configuration_fails_closed(tmp_path, content):
    path = tmp_path / "local.json"
    path.write_text(content)
    with pytest.raises(ConfigError, match="protected sender configuration"):
        ProtectedPolicy(str(path))


def test_missing_policy_file_fails_closed(tmp_path):
    with pytest.raises(ConfigError):
        ProtectedPolicy(str(tmp_path / "missing.json"))


def test_example_policy_parses_and_extends_committed_defaults():
    root = Path(__file__).parents[1]
    policy = ProtectedPolicy(str(root / "examples/protected-senders.example.json"))
    assert policy.check({"from": "relative@example.test"})
    assert policy.check({"from": "news@sub.bank.example.com"})


@pytest.mark.parametrize(
    "identifier",
    ["<one><two>", "one@example.test,two@example.test", "<a@example.test>(hidden)", "<a @b>"],
)
def test_cleanup_rejects_ambiguous_identifiers_before_connecting(settings, identifier):
    service, fake, _ = setup(settings)
    with pytest.raises(MailValidationError, match="Message-ID"):
        service.preview_action("archive", ids=[identifier])
    assert fake.calls == []


@pytest.mark.parametrize("identifier", ["<one><two>", "<a@b> (hidden)"])
def test_ambiguous_message_id_header_cannot_receive_token(settings, identifier):
    service, fake, _ = setup(settings)
    raw = fake.folders["INBOX"]["1"]
    raw.headers = raw.headers.replace(b"<1@example.test>", identifier.encode())
    result = service.preview_action("archive", query={})
    assert result["token"] is None and result["excluded_count"] == 1
    assert result["excluded_reasons"] == {"invalid_identity": 1}


def test_protected_sender_in_another_membership_blocks_preview(settings):
    service, fake, _ = setup(settings)
    other = message("<1@example.test>")
    other.headers = other.headers.replace(b"sender@example.com", b"hidden@gmail.com")
    fake.folders["All Mail"]["2"] = other
    result = service.preview_action("archive", query={})
    assert result["token"] is None
    assert "Personal mail provider" in result["blocked"][0]["reason"]


def test_inconsistent_sender_across_memberships_refuses(settings):
    service, fake, _ = setup(settings)
    other = message("<1@example.test>")
    other.headers = other.headers.replace(b"sender@example.com", b"different@example.com")
    fake.folders["All Mail"]["2"] = other
    result = service.preview_action("archive", query={})
    assert result["token"] is None and result["excluded_ids"] == ["<1@example.test>"]
    assert result["excluded_reasons"] == {"inconsistent_sender_headers": 1}


@pytest.mark.parametrize("operation", ["execute", "undo"])
def test_concurrent_mutation_refuses_promptly_without_waiting(settings, operation):
    service, fake, backend = setup(settings)
    token = service.preview_action("archive", query={})["token"]
    before = len(fake.calls)
    with service._mutation_lock:
        with pytest.raises(MailValidationError, match="Another cleanup operation"):
            if operation == "execute":
                service.execute("archive", token)
            else:
                service.undo_action("offline-log-id")
    assert len(fake.calls) == before and backend.applied == []


def test_expiry_during_revalidation_refuses_before_mutation(settings, monkeypatch):
    clock = [10.0]
    service, _, backend = setup(settings, clock=lambda: clock[0])
    token = service.preview_action("archive", query={})["token"]
    original = service._resolve

    def slow_resolve(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 600
        return result

    monkeypatch.setattr(service, "_resolve", slow_resolve)
    with pytest.raises(MailValidationError, match="expired"):
        service.execute("archive", token)
    assert backend.applied == []


@pytest.mark.parametrize("token", [None, "", "forged-token", "é" * 43, "x" * 257])
def test_absent_or_forged_token_refuses_without_connection(settings, token):
    service, fake, backend = setup(settings)
    with pytest.raises(MailValidationError, match="Invalid or already used"):
        service.execute("archive", token)
    assert fake.calls == [] and backend.applied == []


def test_over_cap_preview_reports_count_even_for_duplicate_ids(settings):
    service, fake, _ = setup(settings, count=201)
    for item in fake.folders["INBOX"].values():
        item.headers = fake.folders["INBOX"]["1"].headers
    result = service.preview_action("archive", query={})
    assert result["count"] == 201 and result["token"] is None


def test_multiple_missing_message_ids_report_blocked_rows(settings):
    service, fake, _ = setup(settings, count=2)
    for uid, item in fake.folders["INBOX"].items():
        item.headers = item.headers.replace(f"Message-ID: <{uid}@example.test>\r\n".encode(), b"")
    result = service.preview_action("archive", query={})
    assert result["count"] == 0 and result["token"] is None
    assert result["excluded_count"] == 1 and result["excluded_ids"] == [""]
    assert result["excluded_reasons"] == {"invalid_identity": 1}


def test_invalid_identity_in_other_mailbox_copy_blocks_preview(settings):
    service, fake, _ = setup(settings)
    other = copy.deepcopy(fake.folders["INBOX"]["1"])
    other.headers = other.headers.replace(
        b"Message-ID: <1@example.test>\r\n", b"Message-ID: <1@example.test> trailing-junk\r\n"
    )
    fake.folders["All Mail"]["2"] = other
    result = service.preview_action("archive", query={})
    assert result["token"] is None
    assert result["excluded_ids"] == ["<1@example.test>"]
    assert result["excluded_reasons"] == {"invalid_identity": 1}
