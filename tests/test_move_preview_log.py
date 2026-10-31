"""Move approvals have their own durable, read-only preview record."""

import hashlib
import json
import re
from pathlib import Path

import pytest
from test_cleanup_guard import settings as settings
from test_cleanup_guard import setup

from proton_mail_mcp.action_log import ActionLog, LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError


@pytest.mark.parametrize(
    "action,params",
    [
        ("archive", None),
        ("move_to_trash", None),
        ("move_to_folder", {"folder": "Folders/Reviewed"}),
    ],
)
def test_move_preview_returns_its_durable_log_id(settings, action, params):
    service, fake, backend = setup(settings)
    fake.folders["Folders/Reviewed"] = {}

    preview = service.preview_action(action, ids=["<1@example.test>"], params=params)

    assert re.fullmatch(r"[0-9a-f]{32}", preview["log_id"])
    [record] = [
        json.loads(line) for line in Path(settings.action_log_path).read_text().splitlines()
    ]
    assert record["log_id"] == preview["log_id"]
    assert record["action"] == "preview_action" and record["params"] == {"action": action}
    assert record["status"] == "previewed" and record["count"] == 1
    assert record["action_hash"] == preview["action_hash"]
    assert record["token_digest"] == hashlib.sha256(preview["token"].encode("ascii")).hexdigest()
    assert preview["token"] not in json.dumps(record)
    assert record["messages"] == [] and backend.applied == []
    assert not {"MOVE", "STORE", "EXPUNGE"} & {call[0] for call in fake.calls}
    [listed] = ActionLog(settings.action_log_path).list_actions(token=preview["token"])["actions"]
    assert listed["log_id"] == preview["log_id"] and listed["preview_action"] == action
    assert ActionLog(settings.action_log_path).list_actions(action=action)["actions"] == [listed]


def test_empty_move_preview_has_a_log_id(settings):
    service, _, backend = setup(settings, count=0)

    preview = service.preview_action("archive", query={})

    assert preview["token"] is None and backend.applied == []
    [record] = [
        json.loads(line) for line in Path(settings.action_log_path).read_text().splitlines()
    ]
    assert record["log_id"] == preview["log_id"] and record["status"] == "empty"


def test_failed_move_preview_log_revokes_its_token(settings, monkeypatch):
    service, _, backend = setup(settings)
    issued = []
    original = service.tokens.issue

    def issue(*args, **kwargs):
        result = original(*args, **kwargs)
        issued.append(result[0])
        return result

    def fail(self, record):
        raise MailError("Synthetic preview append failure")

    monkeypatch.setattr(service.tokens, "issue", issue)
    monkeypatch.setattr(LogTransaction, "append", fail)

    with pytest.raises(MailError, match="Synthetic preview append failure"):
        service.preview_action("archive", query={})

    assert len(issued) == 1 and backend.applied == []
    with pytest.raises(MailValidationError, match="already used"):
        service.tokens.inspect(issued[0], "archive")
