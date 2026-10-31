"""Give the existing recording mutator durable run evidence for resume assertions."""

from proton_mail_mcp.action_log import ActionLog, new_record


def journal_recording_mutator(monkeypatch, backend, path):
    """Journal the recorder's actual frozen rows without changing its mutations."""
    log = ActionLog(path)
    monkeypatch.setattr(backend, "log", log, raising=False)
    original = backend.apply

    def apply(connection, deadline, action, params, rows, **kwargs):
        authorize = kwargs["before_mutation"]
        context = backend.run_context
        record = new_record(action, params)
        record.update(
            status="in_progress",
            token_digest=kwargs.get("token_digest"),
            **{key: context[key] for key in ("run_id", "chunk_index", "approved_total")},
        )
        with log.transaction() as transaction:

            def before_mutation():
                record["messages"] = [
                    {
                        "source_folder": row["folder"],
                        "uidvalidity_before": row["uidvalidity"],
                        "uid_before": row["uid"],
                        "message_id": row["message_id"],
                        "status": "not_attempted",
                    }
                    for row in rows
                ]
                transaction.append(record)
                authorize()
                log.ensure_run(transaction, context)

            result = original(
                connection,
                deadline,
                action,
                params,
                rows,
                **{**kwargs, "before_mutation": before_mutation},
            )
            # The recorder already changed exactly these rows. Attach identity
            # only after comparing its reported outcome to its captured batch.
            assert len(record["messages"]) == result["count"] == len(backend.applied[-1][2])
            messages = []
            for index, (entry, outcome) in enumerate(
                zip(record["messages"], result["messages"], strict=True)
            ):
                assert entry["message_id"] == outcome["message_id"]
                entry = {**entry, "status": outcome["status"]}
                messages.append(entry)
                transaction.append(
                    {
                        "action": "execute_progress",
                        "action_log_id": record["log_id"],
                        "index": index,
                        "message": entry,
                    }
                )
            transaction.append(
                {
                    "action": "execute_result",
                    "action_log_id": record["log_id"],
                    "partial": result["partial"],
                }
            )
            log.update_run(transaction, context["run_id"])
            return {**result, "log_id": record["log_id"], "messages": messages}

    monkeypatch.setattr(backend, "apply", apply)
    return log
