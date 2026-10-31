"""Resumable discovery for rules previews; these rows never authorize changes."""

from __future__ import annotations

from typing import Any

from .cleanup_lookup import (
    _verify_inventory,
    display_value,
    enumerate_uids,
    envelope_row,
    fetch_rows,
)
from .mail import MailValidationError, _quote
from .search import _uidvalidity
from .sender_matching import sender_matches

PREVIEW_SECONDS = 20.0
DISCOVERY_BATCH = 1000
MAX_PREVIEWS = 4


def rule_node(rule: Any) -> tuple:
    def condition_node(field, value):
        # Rules compare To against ENVELOPE addresses, while the header index
        # stores the raw To field. Treat that predicate as unknown so quoting
        # or group syntax cannot let the index exclude an envelope match.
        return ("envelope_to" if field == "to" else field), value

    if hasattr(rule, "spec"):
        # Whole-address / whole-subject equality implies substring membership.
        # This is only a conservative index filter; matches() decides below.
        return (
            "and" if rule.spec.match == "all" else "or",
            tuple(
                condition_node(condition.field, condition.value)
                for condition in rule.spec.conditions
            ),
        )
    return condition_node(rule.field, rule.contains)


def matches(rule: Any, row: dict) -> bool:
    if hasattr(rule, "matches"):
        return rule.matches(row)
    if rule.field == "from":
        return sender_matches(row["from"], rule.contains, decoded=True)
    return rule.contains.casefold() in display_value(row, rule.field).casefold()


def rule_criteria(rule):
    """Retain the existing Bridge narrowing and its literal transport semantics."""
    from .guarded import _address_criteria

    if hasattr(rule, "criteria"):
        return rule.criteria()
    if rule.field == "to":
        return list(_address_criteria("TO", rule.contains)), None
    if rule.field == "subject":
        if rule.contains.isascii():
            return ["SUBJECT", _quote(rule.contains)], None
        return ["SUBJECT"], rule.contains.encode("utf-8")
    return [], None


def discover(client, connection, deadline, rules, folder, state, order):
    """Read cheap envelopes once, choose globally ordered matches before raw audits.

    A continuation reconciles the live source UID set, retaining evidence for
    surviving immutable UIDs. Persist only completed FETCH batches; a timed-out
    stream is never reusable evidence.
    Search-index filtering keeps unknown headers and never catches up inline.
    """
    if not rules:
        return []
    raw_from = any(
        any(condition.field == "from" for condition in rule.spec.conditions)
        if hasattr(rule, "spec")
        else rule.field == "from"
        for rule in rules
    )
    batch_size = max(1, (200 if raw_from else DISCOVERY_BATCH) // state.get("batch_scale", 1))
    snapshot = client._select(connection, folder)
    validity = _uidvalidity(connection)
    uids = enumerate_uids(client, connection, deadline, folder, validity, "rules discovery")
    source = state.setdefault("source", {})
    if source:
        if source["validity"] != validity:
            raise MailValidationError("Rules preview mailbox changed; preview again")
        if source["uids"] != uids:
            previous, live = set(source["uids"]), set(uids)
            added = live - previous
            # Saved SEARCH results concern immutable old UIDs. New arrivals
            # are possible matches for every completed selector; their actual
            # rule predicates are still checked after fetching their rows.
            source["selectors"] = [(selected & live) | added for selected in source["selectors"]]
            source["matches"] = [row for row in source["matches"] if row["uid"] in live]
            if "candidates" in source:
                done = source["candidates"][: source["offset"]]
                pending = source["candidates"][source["offset"] :]
                done = [uid for uid in done if uid in live]
                pending = [uid for uid in pending if uid in live]
                arrivals = client.search_index.candidates(
                    folder,
                    validity,
                    [uid for uid in uids if uid in added],
                    [("or", tuple(rule_node(rule) for rule in rules))],
                )
                source["candidates"] = done + pending + arrivals
                source["offset"] = len(done)
            source.update(uids=uids, complete=False)
    else:
        source.update(validity=validity, uids=uids, selectors=[], offset=0, matches=[])
    indexed = (
        client.search_index.covers(folder, validity, uids)
        if len(source["selectors"]) < len(rules)
        else False
    )
    # Save completed rule selectors too: slow Bridge SEARCH commands must not
    # make every continuation restart discovery at the first rule.
    while len(source["selectors"]) < len(rules):
        criteria, literal = rule_criteria(rules[len(source["selectors"])])
        selected = (
            set(client._search(connection, criteria, literal=literal)) & set(uids)
            if criteria and uids and not indexed
            else set(uids)
        )
        source["selectors"].append(selected)
        state["progress"] = state.get("progress", 0) + 1
    if "candidates" not in source:
        possible = set().union(*source["selectors"])
        source["candidates"] = client.search_index.candidates(
            folder,
            validity,
            [uid for uid in uids if uid in possible],
            [("or", tuple(rule_node(rule) for rule in rules))],
        )
        state["progress"] = state.get("progress", 0) + 1
    while source["offset"] < len(source["candidates"]):
        deadline.check(connection)
        batch = source["candidates"][source["offset"] : source["offset"] + batch_size]
        metadata = client._metadata(
            connection,
            batch,
            deadline=deadline,
            headers=False,
            bodystructure=False,
            internaldate=True,
            envelope=True,
            size=True,
        )
        if set(metadata) != set(batch) or _uidvalidity(connection) != validity:
            raise MailValidationError("Rules preview mailbox changed; preview again")
        rows = [envelope_row(metadata[uid], uid, folder, validity) for uid in batch]
        if raw_from:
            # From matching has always used the parsed raw header. An ENVELOPE
            # miss cannot exclude a matching raw From, including contradictory
            # sender evidence which must still fail the strict rules review.
            rows = fetch_rows(client, connection, deadline, folder, batch, validity)
        found = []
        for row in rows:
            for index, rule in enumerate(rules, start=1):
                if row["uid"] in source["selectors"][index - 1] and matches(rule, row):
                    row["rule_index"] = index
                    found.append(row)
                    break
        source["matches"].extend(found)
        source["offset"] += len(batch)
        state["progress"] = state.get("progress", 0) + len(batch)
    _verify_inventory(client, connection, snapshot, validity, "rules discovery", folder)
    deadline.check(connection)
    source["complete"] = True
    return sorted(
        source["matches"],
        key=lambda row: (row["internaldate"], int(row["uid"])),
        reverse=order == "newest",
    )


def source_fingerprint(rows):
    """Compare immutable discovery evidence with freshly verified raw sources."""
    from .guarded import _fingerprint

    keys = (
        "folder",
        "uidvalidity",
        "uid",
        "message_id",
        "from",
        "to",
        "subject",
        "date",
        "sender",
        "reply_to",
        "size",
        "internaldate",
        "rule_index",
    )
    return _fingerprint([{key: display_value(row, key) for key in keys} for row in rows])
