"""Background rebuilds warm each real folder's independent header handles."""

import threading
from dataclasses import replace

import pytest
from gluon_search import GluonSearchImap
from test_mail import client, message
from test_mail import settings as settings
from test_reindex import assert_no_candidate, completed, seed_old

from proton_mail_mcp.reindex import ReindexService


class FolderReindexImap(GluonSearchImap):
    def __init__(self, count=3, *, block_inbox=False, fail_inbox=False, arrive=False):
        items = {str(uid): message(f"<{uid}@example.com>") for uid in range(1, count + 1)}
        items["1"] = message("<old@example.com>", internaldate="01-Jan-2020 00:00:00 +0000")
        items["1"].headers = items["1"].headers.replace(
            b"sender@example.com", b"info@studio.example.com"
        )
        super().__init__(
            {
                "INBOX": items,
                "All Mail": {str(int(uid) + 10_000): item for uid, item in items.items()},
                "Labels/Only a view": {"50000": message("<label@example.com>")},
            },
            uidnext=True,
        )
        self.block_inbox = block_inbox
        self.fail_inbox = fail_inbox
        self.arrive = arrive
        self.inbox_entered = threading.Event()
        self.release = threading.Event()

    def uid(self, command, *args):
        if command == "FETCH" and self.current == "INBOX":
            self.inbox_entered.set()
            if self.block_inbox:
                assert self.release.wait(5), "test did not release its fake INBOX FETCH"
            if self.fail_inbox:
                raise RuntimeError("private-folder-header-must-not-escape")
            if self.arrive:
                self.folders["All Mail"]["20000"] = message("<arrival@example.com>")
        return super().uid(command, *args)


def folder_service(settings, fake):
    reader = client(replace(settings, reindex_batch_sleep=0.01), fake)
    return reader, ReindexService(reader)


def test_background_reindex_finds_old_inbox_sender_after_indexing_all_mail(settings):
    fake = FolderReindexImap(1105)
    reader, service = folder_service(settings, fake)
    service.execute(service.preview()["token"])
    status = completed(service)

    assert status["state"] == "succeeded"
    assert status["last_full_run"]["new_count"] == status["last_full_run"]["all_mail_count"] == 1105
    assert reader.search_index.known_count("INBOX", "1") == 1105
    assert reader.search_index.coverage("INBOX") == ("1", 1105, "1106")
    assert reader.search_index.known_uids("Labels/Only a view", "1") == set()
    fake.calls.clear()

    result = reader.search_mail(from_="info@studio.example.com", include_snippets=False)

    assert [item["uid"] for item in result["messages"]] == ["1"]
    assert result["total"] == 1 and result["total_is_exact"]
    assert not result["partial"]
    assert "partial_reason" not in result
    assert {
        uid
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
        for uid in call[1].split(",")
    } == {"1"}


def test_cancel_while_warming_real_folder_preserves_serving_index(settings):
    fake = FolderReindexImap(block_inbox=True)
    reader, service = folder_service(replace(settings, reindex_batch_size=2), fake)
    original = seed_old(reader)
    preview = service.preview()
    # ETA includes both handles of each message, while public counts stay All Mail.
    assert preview["eta_seconds"] == service._estimate(6)
    assert preview["eta_seconds"] > service._estimate(3)
    run = service.execute(preview["token"])
    assert fake.inbox_entered.wait(3)
    try:
        running = service.status()
        assert (
            running["last_full_run"]["new_count"] == running["last_full_run"]["all_mail_count"] == 3
        )
        assert running["percent_done"] == 50
        service.cancel(run["run_id"])
    finally:
        fake.release.set()

    assert completed(service)["state"] == "cancelled"
    assert reader.search_index.path.read_bytes() == original
    assert reader.search_index.generation == 0
    assert_no_candidate(reader)


@pytest.mark.parametrize("arrive", [False, True])
def test_folder_warming_failure_or_late_all_mail_arrival_preserves_index(settings, arrive):
    fake = FolderReindexImap(fail_inbox=not arrive, arrive=arrive)
    reader, service = folder_service(settings, fake)
    original = seed_old(reader)
    service.execute(service.preview()["token"])
    status = completed(service)

    assert status["state"] == "failed"
    assert status["last_error"] == ("coverage_below_99_percent" if arrive else "rebuild_failed")
    assert reader.search_index.path.read_bytes() == original
    assert reader.search_index.generation == 0
    assert "private-folder-header" not in service.log.path.read_text()
    assert_no_candidate(reader)
