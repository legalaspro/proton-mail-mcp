"""Offline full-package baseline comparison; run with PYTHONPATH=<src>:tests.

Only AF_UNIX socket peers are used. This script prints counts/timings/outcomes,
never message identifiers or approval tokens. Run under the job's netiso runner.
"""

import argparse
import json
import tempfile
from pathlib import Path

from bridge_costs import DEFAULT_COSTS, PESSIMISTIC_COSTS, ZERO_COSTS, ChargedThreading
from test_preview_proton_shape import BUDGET, SYNTHETIC_BATCH, ProtonShapePeer, WorkClock, headers

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient
from proton_mail_mcp.security import Credentials


def scenarios():
    return (
        "ids-1",
        "ids-2",
        "ids-27",
        "day-INBOX",
        "day-Spam",
        "day-Trash",
        "archive",
        "remove_label",
    )


def run(scenario, mode, label, *, identifiers=SYNTHETIC_BATCH):
    costs = (
        ZERO_COSTS
        if mode == "count"
        else PESSIMISTIC_COSTS
        if mode == "pessimistic"
        else DEFAULT_COSTS
    )
    clock = WorkClock()
    peer = ProtonShapePeer(clock, identifiers, costs=costs)
    if scenario == "day-Trash":
        peer.folders["Trash"]["1"] = peer.folders["INBOX"].pop("1")
    if scenario == "remove_label":
        peer.folders["Labels/Review"] = {
            str(index + 1): headers(identifier) for index, identifier in enumerate(identifiers)
        }
    factory = peer.factory()
    original_time, original_threading = mail.time, mail.threading
    old_socket, old_tls = mail._DeadlineIMAP4._create_socket, mail._DeadlineIMAP4.starttls
    mail.time, mail.threading = clock, ChargedThreading(peer.work)
    if mode != "count":
        mail._DeadlineIMAP4._create_socket = factory._create_socket
        mail._DeadlineIMAP4.starttls = factory.starttls
        factory = mail._DeadlineIMAP4
    try:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                token="offline-model-" + "x" * 32,
                bridge_user="reader@example.invalid",
                imap_port=31337,
                action_log_path=str(Path(tmp) / "actions.jsonl"),
                timeout=30,
                call_timeout=300 if mode == "count" else BUDGET,
            )
            client = MailClient(
                settings, lambda _: Credentials("reader@example.invalid", "dummy"), factory
            )
            action = "move_to_trash"
            if scenario == "report2":
                result = client.search_mail(folder="All Mail", include_snippets=False)
                result = {"count": len(result["messages"]), "partial": result["partial"]}
            else:
                if scenario.startswith("ids-"):
                    kwargs = {"ids": identifiers[: int(scenario.split("-")[1])]}
                elif scenario.startswith("day-"):
                    folder = scenario[4:]
                    kwargs = {
                        "query": {"folder": folder, "since": "2026-10-17", "before": "2026-10-18"}
                    }
                    if folder == "Trash":
                        action = "archive"
                elif scenario == "archive":
                    action, kwargs = "archive", {"ids": identifiers}
                else:
                    action, kwargs = (
                        "remove_label",
                        {"query": {"folder": "Labels/Review"}, "params": {"label": "Review"}},
                    )
                result = client.preview_action(action, **kwargs)
            summary = {
                "label": label,
                "mode": mode,
                "scenario": scenario,
                "token": bool(result.get("token")),
                "count": result["count"],
                "partial": result["partial"],
                "seconds": round(clock.elapsed, 6),
                "budget_margin_seconds": round(BUDGET - clock.elapsed, 6),
                **peer.work.summary(),
            }
            assert peer.mutations == 0
            return summary
    finally:
        peer.close()
        mail.time, mail.threading = original_time, original_threading
        mail._DeadlineIMAP4._create_socket, mail._DeadlineIMAP4.starttls = old_socket, old_tls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--mode", choices=("count", "calibrated", "pessimistic"), required=True)
    parser.add_argument("--scenario", choices=(*scenarios(), "report2", "all"), default="all")
    parser.add_argument("--expect", choices=("success", "timeout", "report2"))
    args = parser.parse_args()
    for scenario in scenarios() if args.scenario == "all" else (args.scenario,):
        result = run(scenario, args.mode, args.label)
        print(json.dumps(result), flush=True)
        if args.expect == "success":
            assert result["token"] and not result["partial"] and result["seconds"] < BUDGET
            assert result["literal_loads"] == 0
        elif args.expect == "timeout":
            assert not result["token"] and result["partial"] and result["seconds"] >= BUDGET
        elif args.expect == "report2":
            assert result["count"] == 0 and result["partial"] and result["seconds"] >= BUDGET


if __name__ == "__main__":
    main()
