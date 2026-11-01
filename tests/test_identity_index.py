"""Protocol certification and private storage, using only temporary state."""

import json
import os
import re
import signal
from dataclasses import replace
from types import SimpleNamespace

import pytest

from proton_mail_mcp import identity_index, localfs
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import CallDeadlineExceeded


class Deadline:
    expired = False

    def check(self, connection=None):
        if self.expired:
            raise CallDeadlineExceeded("Inventory deadline reached")


class Connection:
    capabilities = (b"IMAP4REV1", b"CONDSTORE")

    def __init__(self):
        self.rows = {
            "1": {
                "uid": "1",
                "message_id": "<one@example.invalid>",
                "_raw_identity_fields": ["<one@example.invalid>"],
            }
        }
        self.validity, self.next_uid, self.modseq = 41, 2, 1
        self.changed = {}
        self.commands = []
        self.fetches = []
        self.status_hook = None

    def status(self, folder, fields):
        self.commands.append(("STATUS", folder, fields))
        if self.status_hook:
            self.status_hook(self)
        raw = (
            f'"INBOX" (UIDVALIDITY {self.validity} UIDNEXT {self.next_uid} '
            f"HIGHESTMODSEQ {self.modseq} MESSAGES {len(self.rows)})"
        ).encode()
        return "OK", [raw]

    def uid(self, command, sequence, message_parts):
        match = re.fullmatch(r"(\(UID MODSEQ\)) (\(CHANGEDSINCE ([1-9][0-9]*)\))", message_parts)
        assert match is not None
        fields, modifier, threshold = match.groups()
        assert (command, sequence, fields) == ("FETCH", "1:*", "(UID MODSEQ)")
        self.commands.append((command, sequence, fields, modifier))
        since = int(threshold)
        return "OK", [
            f"1 (UID {uid} MODSEQ ({modseq}))".encode()
            for uid, modseq in self.changed.items()
            if modseq > since and uid in self.rows
        ]


@pytest.fixture
def inventory(tmp_path):
    settings = Settings(
        token="x" * 32,
        bridge_user="synthetic@example.invalid",
        identity_index_dir=str(tmp_path / "index"),
    )
    connection = Connection()
    client = SimpleNamespace(
        settings=settings, _search=lambda _connection, _criteria: list(connection.rows)
    )
    deadline = Deadline()

    def fetch(_client, peer, _deadline, _folder, uids, _validity, *, raw_fields):
        assert raw_fields
        peer.fetches.append(list(uids))
        return [peer.rows[uid].copy() for uid in uids]

    def run():
        return identity_index.certified_inventory(
            client, connection, deadline, "INBOX", str(connection.validity), fetch_rows=fetch
        )

    return SimpleNamespace(
        run=run, connection=connection, client=client, deadline=deadline, path=tmp_path / "index"
    )


def test_cold_complete_inventory_and_warm_no_raw_fetch(inventory):
    assert len(inventory.run()) == 1
    assert inventory.connection.fetches == [["1"]]
    assert len(inventory.run()) == 1
    assert inventory.connection.fetches == [["1"]]
    assert not any("CHANGEDSINCE" in command[-1] for command in inventory.connection.commands)
    files = list(inventory.path.iterdir())
    assert files and all(localfs.mode_private(path.stat()) for path in files)
    text = next(inventory.path.glob("*.json")).read_text()
    assert '"subject"' not in text and '"from"' not in text and '"body"' not in text
    assert "synthetic@example.invalid" not in text


def test_authenticated_string_capabilities_enable_index(inventory):
    inventory.connection.capabilities = ("IMAP4REV1", "CONDSTORE")
    assert inventory.run()
    assert list(inventory.path.glob("*.json"))


def test_new_mail_delta_retains_hidden_duplicate(inventory):
    inventory.run()
    peer = inventory.connection
    peer.rows["2"] = {
        "uid": "2",
        "message_id": "<unrelated@example.invalid>",
        "_raw_identity_fields": ["<unrelated@example.invalid>", "<one@example.invalid>"],
        "identity_invalid": True,
    }
    peer.next_uid, peer.modseq, peer.changed = 3, 2, {"2": 2}
    found = inventory.run()
    assert peer.fetches[-1] == ["2"]
    assert found[1]["_raw_identity_fields"] == peer.rows["2"]["_raw_identity_fields"]
    assert found[1]["identity_invalid"]


def test_modseq_delta_refreshes_existing_uid(inventory):
    inventory.run()
    peer = inventory.connection
    peer.modseq, peer.changed = 2, {"1": 2}
    assert inventory.run()
    assert peer.fetches[-1] == ["1"]
    assert any("CHANGEDSINCE" in command[-1] for command in peer.commands)


def test_expunge_reconciles_uid_set(inventory):
    inventory.run()
    peer = inventory.connection
    peer.rows.clear()
    peer.modseq = 2
    assert inventory.run() == []
    assert peer.fetches == [["1"]]
    stored = json.loads(next(inventory.path.glob("*.json")).read_text())
    assert stored["inventory"]["rows"] == {}


@pytest.mark.parametrize("attribute,value", [("validity", 42), ("next_uid", 1), ("modseq", 0)])
def test_regressed_or_changed_generation_never_reuses(inventory, attribute, value):
    inventory.run()
    setattr(inventory.connection, attribute, value)
    result = inventory.run()
    assert result is None or inventory.connection.fetches[-1] == ["1"]


def test_changed_uidvalidity_rebuilds_raw_fields(inventory):
    inventory.run()
    inventory.connection.validity += 1
    inventory.connection.rows["1"]["_raw_identity_fields"] = ["<different@example.invalid>"]
    assert inventory.run()[0]["_raw_identity_fields"] == ["<different@example.invalid>"]
    assert inventory.connection.fetches[-1] == ["1"]


def test_equal_modseq_changed_uid_set_rebuilds(inventory):
    inventory.run()
    peer = inventory.connection
    peer.rows["2"] = {**peer.rows.pop("1"), "uid": "2"}
    peer.next_uid = 3
    assert inventory.run()[0]["uid"] == "2"
    assert peer.fetches[-1] == ["2"]


@pytest.mark.parametrize("corruption", [b"{", b"[]", b'{"inventory":{},"sha256":"bad"}'])
def test_corrupt_file_rebuilds(inventory, corruption):
    inventory.run()
    next(inventory.path.glob("*.json")).write_bytes(corruption)
    assert inventory.run()
    assert inventory.connection.fetches[-1] == ["1"]


def test_corrupt_schema_with_valid_seal_rebuilds(inventory):
    inventory.run()
    path = next(inventory.path.glob("*.json"))
    sealed = json.loads(path.read_text())
    sealed["inventory"]["rows"]["1"]["_raw_identity_fields"] = "invalid"
    sealed["sha256"] = identity_index.hashlib.sha256(
        identity_index._payload(sealed["inventory"])
    ).hexdigest()
    path.write_bytes(identity_index._payload(sealed))
    assert inventory.run()
    assert inventory.connection.fetches[-1] == ["1"]


def test_rfc_only_inventory_verdict_is_rebuilt_for_opaque_identity(inventory, monkeypatch):
    row = inventory.connection.rows["1"]
    row.update(
        message_id="Provider-ID",
        _raw_identity_fields=["Provider-ID"],
        identity_invalid=True,
    )
    with monkeypatch.context() as old_parser:
        old_parser.setattr(identity_index, "_VERSION", 2)
        assert inventory.run()[0]["identity_invalid"]
    row["identity_invalid"] = False
    assert inventory.run()[0]["identity_invalid"] is False
    assert inventory.connection.fetches == [["1"], ["1"]]


@pytest.mark.parametrize("target", ["directory", "file", "lock"])
def test_permissive_storage_never_reused(inventory, target):
    inventory.run()
    path = (
        inventory.path
        if target == "directory"
        else next(inventory.path.glob("*.json" if target == "file" else "*.lock"))
    )
    path.chmod(0o777 if target == "directory" else 0o666)
    assert inventory.run() is None
    assert inventory.connection.fetches == [["1"]]


def test_wrong_owner_storage_refuses(inventory, monkeypatch):
    inventory.run()
    monkeypatch.setattr(identity_index.os, "getuid", lambda: os.stat(inventory.path).st_uid + 1)
    assert inventory.run() is None


def test_symlink_storage_refuses(inventory, tmp_path):
    inventory.run()
    path = next(inventory.path.glob("*.json"))
    target = tmp_path / "saved.json"
    path.rename(target)
    path.symlink_to(target)
    assert inventory.run() is None


def test_fifo_inventory_refuses_without_blocking(inventory):
    inventory.run()
    path = next(inventory.path.glob("*.json"))
    path.unlink()
    os.mkfifo(path, 0o600)

    def blocked(_signum, _frame):
        raise AssertionError("Opening non-regular inventory blocked")

    previous_handler = signal.signal(signal.SIGALRM, blocked)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        assert inventory.run() is None
        assert inventory.connection.fetches == [["1"]]
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.parametrize("enabled,capabilities", [(False, (b"CONDSTORE",)), (True, ())])
def test_unsupported_or_disabled_never_opens_state(inventory, enabled, capabilities):
    inventory.client.settings = replace(inventory.client.settings, identity_index_enabled=enabled)
    inventory.connection.capabilities = capabilities
    assert inventory.run() is None
    assert not inventory.path.exists()


def test_account_scope_does_not_reuse_another_account(inventory):
    inventory.run()
    inventory.client.settings = replace(
        inventory.client.settings, bridge_user="other@example.invalid"
    )
    assert inventory.run()
    assert inventory.connection.fetches == [["1"], ["1"]]
    assert len(list(inventory.path.glob("*.json"))) == 2


def test_drift_between_snapshots_never_returns_or_persists(inventory):
    peer = inventory.connection
    peer.status_hook = lambda current: setattr(current, "modseq", current.modseq + 1)
    assert inventory.run() is None
    assert not list(inventory.path.glob("*.json"))


def test_warm_drift_does_not_approve_saved_inventory(inventory):
    inventory.run()
    original = next(inventory.path.glob("*.json")).read_bytes()
    inventory.connection.status_hook = lambda current: setattr(
        current, "modseq", current.modseq + 1
    )
    assert inventory.run() is None
    assert next(inventory.path.glob("*.json")).read_bytes() == original


def test_drift_after_delta_fetch_does_not_persist(inventory):
    inventory.run()
    original = next(inventory.path.glob("*.json")).read_bytes()
    peer = inventory.connection
    peer.modseq, peer.changed = 2, {"1": 2}
    calls = 0

    def drift(current):
        nonlocal calls
        calls += 1
        if calls == 3:
            current.modseq += 1

    peer.status_hook = drift
    assert inventory.run() is None
    assert next(inventory.path.glob("*.json")).read_bytes() == original


@pytest.mark.parametrize("attribute", ["next_uid", "modseq"])
def test_valid_positive_counter_regression_rebuilds(inventory, attribute):
    setattr(inventory.connection, attribute, 10)
    inventory.run()
    setattr(inventory.connection, attribute, 5)
    assert inventory.run()
    assert inventory.connection.fetches == [["1"], ["1"]]


def test_rejected_condstore_command_requests_full_scan(inventory):
    def unsupported(*args):
        raise identity_index.imaplib.IMAP4.error("Unsupported metadata request")

    inventory.connection.status = unsupported
    assert inventory.run() is None
    assert not list(inventory.path.glob("*.json"))


def test_wrong_status_folder_cannot_certify(inventory):
    status = inventory.connection.status
    inventory.connection.status = lambda folder, fields: (
        "OK",
        [status(folder, fields)[1][0].replace(b'"INBOX"', b'"Archive"')],
    )
    assert inventory.run() is None


def test_unreconciled_changed_uid_refuses(inventory):
    inventory.run()
    inventory.connection.modseq = 2
    inventory.connection.uid = lambda *args: ("OK", [b"1 (UID 99 MODSEQ (2))"])
    assert inventory.run() is None


def test_rebuild_deadline_propagates(inventory):
    inventory.deadline.expired = True
    with pytest.raises(CallDeadlineExceeded):
        inventory.run()
    assert not list(inventory.path.glob("*.json"))


def test_index_configuration_defaults_and_overrides():
    env = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "example"}
    assert Settings.from_env(env).identity_index_enabled
    settings = Settings.from_env(
        {
            **env,
            "PROTON_MCP_IDENTITY_INDEX_ENABLED": "0",
            "PROTON_MCP_IDENTITY_INDEX_DIR": "/tmp/test",
        }
    )
    assert not settings.identity_index_enabled
    assert settings.identity_index_dir == "/tmp/test"
    with pytest.raises(ConfigError):
        Settings.from_env({**env, "PROTON_MCP_IDENTITY_INDEX_ENABLED": "yes"})
