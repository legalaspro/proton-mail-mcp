"""Execution-only views for tests that also create durable approval previews."""

import json
from pathlib import Path

from proton_mail_mcp.action_log import replay_records


def execution_records(records, *, include_runs=False):
    return [
        record
        for record in replay_records(records, include_runs=include_runs)
        if record.get("action") != "preview_action"
    ]


def execution_log(path):
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    return execution_records([json.loads(line) for line in lines])
