"""Journal framing failures use scratch files only."""

import errno
import json
import os

import pytest

from proton_mail_mcp.action_log import ActionLog, replay_records
from proton_mail_mcp.mail import MailError

pytestmark = pytest.mark.real_fsync


def read(log):
    with log.transaction(readonly=True) as transaction:
        return transaction.records()


@pytest.mark.parametrize("tail", [b'{"action":', b'{"action":"undo_action"}', b'"\xf0\x9f'])
def test_unterminated_tail_preserves_records_and_next_append(tmp_path, tail):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    prefix = b'{"action":"first"}\n' + tail
    log.path.write_bytes(prefix)
    assert read(log) == [{"action": "first"}]
    assert log.path.read_bytes() == prefix  # Lookup cannot repair or rewrite.
    with log.transaction() as transaction:
        transaction.append({"action": "next"})
        transaction.append({"action": "last"})
    assert log.path.read_bytes().startswith(prefix)
    assert read(log) == [{"action": name} for name in ("first", "next", "last")]


@pytest.mark.parametrize(
    "bad", [b'{"action":\n', b'"\xff"\n', b"[]\n", b"\n", b'{"action":\0bad-seal\n']
)
@pytest.mark.parametrize("suffix", [b"", b'{"action":"later"}\n', b'{"torn":'])
def test_complete_corruption_refuses_read_and_append(tmp_path, bad, suffix):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    content = b'{"action":"first"}\n' + bad + suffix
    log.path.write_bytes(content)
    with pytest.raises(MailError, match="invalid"):
        read(log)
    with pytest.raises(MailError, match="invalid"), log.transaction() as transaction:
        transaction.append({"action": "next"})
    assert log.path.read_bytes() == content


@pytest.mark.parametrize(
    "bad",
    [
        b'{"action":\n',
        b"[]\n",
        b'{"action":"execute_progress","action_log_id":"missing"}\n',
    ],
)
@pytest.mark.parametrize("previous_read", [False, True])
def test_invalid_read_cannot_leave_append_cache(tmp_path, bad, previous_read):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    prefix = b'{"action":"first"}\n'
    log.path.write_bytes(prefix)
    content = prefix + bad + b'{"torn":'
    with log.transaction() as transaction:
        if previous_read:
            assert transaction.records() == [{"action": "first"}]
        log.path.write_bytes(content)
        with pytest.raises(MailError, match="invalid"):
            transaction.records()
        with pytest.raises(MailError, match="invalid"):
            transaction.append({"action": "never_written"})
    assert log.path.read_bytes() == content


@pytest.mark.parametrize("failure", ["short", "no_newline", "zero", "enospc", "fsync"])
def test_failed_append_stops_transaction(tmp_path, monkeypatch, failure):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    first, failed, later = ({"action": name} for name in ("first", "failed", "later"))
    write, fsync = os.write, os.fsync
    writes = []
    with log.transaction() as transaction:
        transaction.append(first)

        def fail_write(descriptor, payload):
            writes.append(payload)
            if failure == "enospc":
                raise OSError(errno.ENOSPC, "synthetic disk full")
            if failure == "zero":
                return 0
            if failure in {"short", "no_newline"}:
                length = len(payload) // 2 if failure == "short" else len(payload) - 1
                return write(descriptor, payload[:length])
            return write(descriptor, payload)

        def fail_sync(descriptor):
            if failure == "fsync":
                raise OSError(errno.EIO, "synthetic sync failure")
            return fsync(descriptor)

        with monkeypatch.context() as patch:
            patch.setattr(os, "write", fail_write)
            patch.setattr(os, "fsync", fail_sync)
            with pytest.raises((MailError, OSError)):
                transaction.append(failed)
            with pytest.raises(MailError):
                transaction.append(later)
        assert writes == [(json.dumps(failed, separators=(",", ":")) + "\n").encode()]
    prefix = log.path.read_bytes()
    # A full frame surviving fsync failure is still evidence; partial JSON is not.
    expected = [first, failed] if failure == "fsync" else [first]
    assert read(log) == expected
    with log.transaction() as transaction:
        transaction.append(later)
    assert read(log) == [*expected, later]
    assert log.path.read_bytes().startswith(prefix)


def test_torn_tail_seal_can_itself_be_interrupted(tmp_path, monkeypatch):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    prefix = b'{"action":"first"}\n{"torn":'
    log.path.write_bytes(prefix)
    write = os.write
    with log.transaction() as transaction, monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda fd, data: write(fd, data[: len(data) // 2]))
        with pytest.raises(MailError):
            transaction.append({"action": "never_written"})
    interrupted_seal = log.path.read_bytes()
    assert interrupted_seal.startswith(prefix) and not interrupted_seal.endswith(b"\n")
    assert read(log) == [{"action": "first"}]
    with log.transaction() as transaction:
        transaction.append({"action": "next"})
    assert log.path.read_bytes().startswith(interrupted_seal)
    assert read(log) == [{"action": "first"}, {"action": "next"}]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_write_stops_transaction(tmp_path, monkeypatch, interruption):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    first, failed, later = ({"action": name} for name in ("first", "failed", "later"))
    write = os.write
    error = interruption("synthetic interruption")

    def interrupt_write(descriptor, payload):
        write(descriptor, payload[: len(payload) // 2])
        raise error

    with log.transaction() as transaction:
        transaction.append(first)
        with monkeypatch.context() as patch:
            patch.setattr(os, "write", interrupt_write)
            with pytest.raises(interruption) as caught:
                transaction.append(failed)
        assert caught.value is error
        prefix = log.path.read_bytes()
        with pytest.raises(MailError, match="unavailable"):
            transaction.append(later)
        assert log.path.read_bytes() == prefix
    assert read(log) == [first]
    with log.transaction() as transaction:
        transaction.append(later)
    assert read(log) == [first, later]
    assert log.path.read_bytes().startswith(prefix)


def test_sealed_tail_corruption_is_not_ignored(tmp_path):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    log.path.write_bytes(b'{"action":"first"}\n{"torn":')
    with log.transaction() as transaction:
        transaction.append({"action": "next"})
    log.path.write_bytes(log.path.read_bytes().replace(b'"torn"', b'"oops"'))
    with pytest.raises(MailError, match="invalid"):
        read(log)


def test_readonly_fifo_is_rejected_without_blocking(tmp_path, monkeypatch):
    log = ActionLog(tmp_path / "synthetic.fifo")
    os.mkfifo(log.path)
    open_file = os.open

    def nonblocking_open(path, flags, mode):
        assert flags & os.O_NONBLOCK  # Fail promptly even if the guard regresses.
        return open_file(path, flags, mode)

    monkeypatch.setattr(os, "open", nonblocking_open)
    with pytest.raises(MailError, match="private regular file"):
        read(log)


@pytest.mark.parametrize("action", ["mark_read", "mark_unread", "move_to_folder"])
@pytest.mark.parametrize(
    "field,changed",
    [
        ("fingerprint", ["<different@example.test>", "date", 10]),
        ("seen_before", True),
        ("destination_before", [{"uid": "9", "uidvalidity": "2"}]),
        ("destination_uidvalidity", "3"),
    ],
)
def test_execution_progress_cannot_replace_undo_identity(action, field, changed):
    entry = {
        "fingerprint": ["<journal@example.test>", "date", 10],
        "seen_before": False,
        "destination_before": [],
        "destination_uidvalidity": "2",
        "status": "not_attempted",
    }
    original = {
        "action": action,
        "log_id": "synthetic-id",
        "status": "in_progress",
        "messages": [entry],
    }
    progress = {
        "action": "execute_progress",
        "action_log_id": "synthetic-id",
        "index": 0,
        "message": {**entry, "status": "done"},
    }
    assert replay_records([original, progress])[0]["messages"][0]["status"] == "done"
    progress["message"][field] = changed
    with pytest.raises(MailError, match="invalid execution identity"):
        replay_records([original, progress])
