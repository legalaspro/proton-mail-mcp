"""Rules discovery preserves immutable UID work across synthetic mailbox changes."""

from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from test_mail import message
from test_mail import settings as settings

from proton_mail_mcp import rules_preview
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.search_index import SearchIndex


class Deadline:
    expired = False

    def check(self, connection=None):
        if self.expired:
            raise CallDeadlineExceeded("Synthetic discovery deadline reached")


@pytest.fixture
def discovery(monkeypatch):
    def metadata(uid, subject="Hello"):
        sender = [[b"Sender", None, b"sender", b"example.com"]]
        return {
            "envelope": [
                b"Wed, 14 Oct 2026 10:00:00 +0000",
                subject.encode(),
                sender,
                sender,
                sender,
                [[None, None, b"reader", b"example.com"]],
                None,
                None,
                None,
                f"<discovery-{uid}@example.test>".encode(),
            ],
            "size": 100,
            "internaldate": int(uid),
        }

    peer = SimpleNamespace(validity="1", rows={uid: metadata(uid) for uid in ("1", "2", "3")})
    deadline = Deadline()
    reads, searches = [], []

    def fetch(connection, uids, **options):
        reads.append(tuple(uids))
        if len(reads) == 1:
            deadline.expired = True
        return {uid: peer.rows[uid] for uid in uids}

    def search(connection, criteria, *, literal):
        searches.append(criteria)
        return list(peer.rows)

    index = SimpleNamespace(
        covers=lambda *args: False, candidates=lambda folder, validity, uids, nodes: uids
    )
    client = SimpleNamespace(
        _select=lambda *args: {}, _metadata=fetch, _search=search, search_index=index
    )
    monkeypatch.setattr(rules_preview, "_uidvalidity", lambda connection: peer.validity)
    monkeypatch.setattr(rules_preview, "enumerate_uids", lambda *args: sorted(peer.rows, key=int))
    monkeypatch.setattr(rules_preview, "_verify_inventory", lambda *args: None)
    rule = SimpleNamespace(field="subject", contains="Hello")
    state = {"batch_scale": 500}

    def run():
        return rules_preview.discover(client, peer, deadline, [rule], "INBOX", state, "oldest")

    return SimpleNamespace(
        peer=peer,
        deadline=deadline,
        reads=reads,
        searches=searches,
        state=state,
        metadata=metadata,
        run=run,
    )


@pytest.mark.parametrize("removed", ["1", "3"])
def test_discovery_reconciles_removed_and_new_uids_without_refetching_survivors(discovery, removed):
    case = discovery
    with pytest.raises(CallDeadlineExceeded):
        case.run()
    assert case.reads == [("1", "2")]
    assert case.state["source"]["offset"] == 2
    del case.peer.rows[removed]
    case.peer.rows["4"] = case.metadata("4")
    case.peer.rows["5"] = case.metadata("5", "Unrelated")
    case.deadline.expired = False

    rows = case.run()

    assert [row["uid"] for row in rows] == [uid for uid in ("1", "2", "3", "4") if uid != removed]
    assert case.state["source"]["uids"] == sorted(case.peer.rows, key=int)
    assert len(case.searches) == 1
    assert all(len(batch) <= 2 for batch in case.reads)
    assert not set(case.reads[0]).intersection(uid for batch in case.reads[1:] for uid in batch)
    assert "5" in {uid for batch in case.reads[1:] for uid in batch}


def test_discovery_can_finish_when_all_saved_uids_are_removed(discovery):
    with pytest.raises(CallDeadlineExceeded):
        discovery.run()
    discovery.peer.rows.clear()
    discovery.deadline.expired = False

    assert discovery.run() == []
    assert discovery.reads == [("1", "2")]
    assert discovery.state["source"]["offset"] == 0


def test_discovery_still_refuses_uidvalidity_change(discovery):
    with pytest.raises(CallDeadlineExceeded):
        discovery.run()
    discovery.peer.validity = "2"
    discovery.deadline.expired = False

    with pytest.raises(MailValidationError, match="mailbox changed"):
        discovery.run()
    assert discovery.reads == [("1", "2")]


@pytest.mark.parametrize("packaged", [True, False])
def test_to_index_predicate_cannot_hide_an_envelope_match(settings, packaged):
    index = SearchIndex(settings)
    headers = BytesParser(policy=policy.default).parsebytes(
        message("<to-index@example.test>").headers
    )
    index.observe("INBOX", "1", {"1": {"headers": headers}})
    assert index.covers("INBOX", "1", ["1"])
    rule = (
        SimpleNamespace(field="to", contains="Envelope Name")
        if packaged
        else SimpleNamespace(
            spec=SimpleNamespace(
                match="all",
                conditions=[
                    SimpleNamespace(field="to", value="Envelope Name"),
                    SimpleNamespace(field="subject", value="Hello"),
                ],
            )
        )
    )
    # A rendered ENVELOPE address may contain a name absent from the raw To
    # field. The discovery prefilter must preserve that possible match.
    node = rules_preview.rule_node(rule)
    assert index.candidates("INBOX", "1", ["1"], [node]) == ["1"]
    if not packaged:
        headers.replace_header("Subject", "Unrelated")
        index.observe("INBOX", "1", {"1": {"headers": headers}})
        assert index.candidates("INBOX", "1", ["1"], [node]) == []
