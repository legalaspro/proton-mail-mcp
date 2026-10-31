"""Private, disposable SQLite index for the decoded-header matcher.

IMAP UIDs identify immutable message content only within a folder's UIDVALIDITY.
Unknown messages and predicates always survive this filter. Full-message TEXT
predicates stay with Bridge; this index stores no body or snippet text.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .sender_matching import _parsed_sender

_LOG = logging.getLogger(__name__)
_VERSION = 2
_COLUMNS = {"subject": "subject", "from": "from_addr", "to": "to_addr"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (folder TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    rowid INTEGER PRIMARY KEY,
    folder TEXT NOT NULL,
    uidvalidity TEXT NOT NULL,
    uid TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    from_addr TEXT NOT NULL DEFAULT '',
    to_addr TEXT NOT NULL DEFAULT '',
    cc TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    thread_ids TEXT,
    headers_known INTEGER NOT NULL DEFAULT 0,
    UNIQUE (folder, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS coverage (
    folder TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL, message_count INTEGER NOT NULL,
    uidnext TEXT NOT NULL, certified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_roles (role TEXT PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_metadata (
    folder TEXT NOT NULL, uidvalidity TEXT NOT NULL, uid TEXT NOT NULL,
    uid_number INTEGER NOT NULL, internaldate REAL, message_id TEXT,
    envelope_digest TEXT, size INTEGER, identifier_known INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS search_metadata_message_id ON search_metadata
    (folder, uidvalidity, message_id);
CREATE INDEX IF NOT EXISTS search_metadata_order ON search_metadata
    (folder, uidvalidity, internaldate DESC, uid_number DESC);
CREATE INDEX IF NOT EXISTS search_metadata_fingerprint ON search_metadata
    (envelope_digest, internaldate, size);
CREATE TRIGGER IF NOT EXISTS messages_metadata_delete AFTER DELETE ON messages BEGIN
    DELETE FROM search_metadata WHERE folder = old.folder
    AND uidvalidity = old.uidvalidity AND uid = old.uid;
END;
CREATE TABLE IF NOT EXISTS index_sync (
    folder TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL, message_count INTEGER NOT NULL,
    max_uid TEXT NOT NULL, observed_at TEXT NOT NULL, last_catchup_at TEXT,
    last_expunge_reconciliation_at TEXT NOT NULL
);
PRAGMA user_version = 2;
"""
# Count known headers in the same read so older cache writers, which do not
# invalidate coverage metadata on header loss, cannot leave a proof.
_PROOF = (
    "SELECT c.uidvalidity, c.message_count, c.uidnext FROM coverage c "
    "JOIN mailboxes m ON m.folder = c.folder AND m.uidvalidity = c.uidvalidity "
    "WHERE c.folder = ? AND c.certified = 1 AND c.message_count = "
    "(SELECT count(*) FROM messages h WHERE h.folder = c.folder "
    "AND h.uidvalidity = c.uidvalidity AND h.headers_known = 1)"
)


class _Unavailable(Exception):
    """The disposable cache cannot safely narrow this search."""


class Unplaced(NamedTuple):
    """A unique All Mail fingerprint no proven real folder holds; holders have a cached row."""

    fingerprint: tuple[str, float, int]
    holders: tuple[str, ...]


_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, IndexError, _Unavailable)


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise _Unavailable("unsafe_permissions")


class SearchIndex:
    """Account-scoped accelerator; all failures retain the caller's original UIDs.

    Rebuilding explicitly discards this account's cache. Ordinary startup checks
    an existing index once; an absent cache is created on the first data write.
    Connections and nonblocking locks are local to each operation, so one client
    can safely serve concurrent threads without sharing SQLite connections.
    """

    def __init__(
        self,
        settings: Any,
        *,
        rebuild: bool = False,
        _path: Path | None = None,
        _candidate: bool = False,
    ):
        from .action_log import default_log_path

        state = Path(settings.action_log_path) if settings.action_log_path else default_log_path()
        account = [settings.bridge_user, settings.imap_host, settings.imap_port]
        key = hashlib.sha256(json.dumps(account, ensure_ascii=True).encode("ascii")).hexdigest()
        self.path = _path or state.parent / "search-index" / (key + ".sqlite3")
        self.enabled = False
        self._initialized = False
        self._warned = False
        self.generation = 0
        self._coverage_lock = threading.RLock()
        self._candidate_lock: int | None = None
        self.reason = "missing"
        try:
            if any(
                (parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
                for parent in self.path.resolve().parents
            ):
                raise _Unavailable("repository_path")
            self.enabled = True
            if _candidate:
                return
            self._initialize(create=rebuild, rebuild=rebuild)
            if not self._initialized:
                self._warn()
        except _ERRORS as error:
            self._failure(error)
            if rebuild:
                # An explicit reset must either obtain its exclusive lock now
                # or report failure; never retry it later as an ordinary open.
                self.enabled = False

    def _initialize(self, *, create: bool = False, rebuild: bool = False) -> None:
        if create:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not self.path.exists():
            return
        with self._locked(exclusive=True, create=create) as directory:
            existing = self._check_files(directory)
            if rebuild:
                for name in existing:
                    os.unlink(name, dir_fd=directory)
                existing = set()
            missing = self.path.name not in existing
            if missing and existing:
                raise _Unavailable("incomplete_files")
            if not missing:
                with closing(self._connect()) as database:
                    version = database.execute("PRAGMA user_version").fetchone()[0]
                    schema = (
                        database.execute(
                            "SELECT name, type, rootpage FROM sqlite_master"
                        ).fetchall()
                        if version == _VERSION
                        else []
                    )
                    # V2 has no virtual tables (tables without a root b-tree).
                    # Inspect metadata without loading their modules: even DROP
                    # requires FTS5 on SQLite builds that omit it.
                    discard = version != _VERSION or any(
                        kind == "table" and not rootpage for _, kind, rootpage in schema
                    )
                    if not discard:
                        expected = {
                            "mailboxes",
                            "messages",
                            "coverage",
                            "index_roles",
                            "search_metadata",
                            "search_metadata_message_id",
                            "search_metadata_order",
                            "search_metadata_fingerprint",
                            "messages_metadata_delete",
                            "index_sync",
                        }
                        if not expected <= {name for name, _, _ in schema}:
                            raise _Unavailable("schema_tables")
                        if "thread_ids" not in {
                            row[1] for row in database.execute("PRAGMA table_info(messages)")
                        }:
                            database.execute("ALTER TABLE messages ADD COLUMN thread_ids TEXT")
                if discard:
                    # Close SQLite before unlinking, then check again: closing
                    # can remove sidecars. Keep the exclusive file lock held.
                    for name in self._check_files(directory):
                        os.unlink(name, dir_fd=directory)
                    _LOG.warning("Old search index cache discarded; rebuilding from scratch")
                    missing = create = True
            if missing:
                if not create:
                    return
                descriptor = os.open(
                    self.path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                os.close(descriptor)
                with closing(self._connect()) as database, database:
                    database.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
            self._initialized = True
            self.reason = None

    def _warn(self) -> None:
        if not self._warned:
            self._warned = True
            _LOG.warning("Search index fallback (%s); using the existing search path", self.reason)

    def _failure(self, error: Exception) -> None:
        # Never log exception strings: SQLite and filesystem errors can include
        # account paths or indexed content. A busy peer is transient, including
        # startup's exclusive initialization lock; retry next use.
        if isinstance(error, BlockingIOError) or getattr(error, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            reason = "busy"
        elif isinstance(error, _Unavailable):
            reason = str(error)
        elif isinstance(error, FileNotFoundError):
            reason = "missing"
        elif isinstance(error, PermissionError):
            reason = "permission_denied"
        elif getattr(error, "sqlite_errorcode", 0) & 255 in {
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        }:
            reason = "corrupt"
        elif isinstance(error, sqlite3.Error):
            reason = "database_error"
        else:
            reason = "storage_error"
        self.reason = reason
        self.enabled = reason == "busy"
        self._warn()

    @contextmanager
    def _locked(
        self, *, exclusive: bool = False, create: bool = False, retain: bool = False
    ) -> Iterator[int]:
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            _private(os.fstat(directory), directory=True)
            if self._candidate_lock is not None:
                # The worker retains this exclusive lease across batches, so a
                # different process's startup sweep cannot reclaim its files.
                yield directory
                return
            lock = os.open(
                self.path.name + ".lock",
                os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
                0o600,
                dir_fd=directory,
            )
            _private(os.fstat(lock))
            fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            if retain:
                self._candidate_lock = lock
            yield directory
        finally:
            if lock is not None and lock != self._candidate_lock:
                os.close(lock)
            os.close(directory)

    def _check_files(self, directory: int) -> set[str]:
        found = set()
        for suffix in ("", "-journal", "-wal", "-shm"):
            name = self.path.name + suffix
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _private(info)
            found.add(name)
        return found

    def _connect(self) -> sqlite3.Connection:
        # mode=rw refuses a file removed after startup, rather than silently
        # creating a replacement with no evidence for cached negative results.
        database = sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True, timeout=0)
        try:
            # This is a disposable accelerator, never durable mail or action
            # evidence. Avoid an fsync for every observed header batch;
            # SQLite errors reject interrupted/corrupt cache writes.
            database.execute("PRAGMA synchronous = OFF")
            return database
        except sqlite3.Error:
            database.close()
            raise

    def _run(
        self,
        operation: Callable[[sqlite3.Connection], Any],
        default: Any = None,
        *,
        create: bool = False,
    ) -> Any:
        # Proof installation and any writes which can invalidate it share this
        # lock. Reentrancy lets a failed operation discard its proof safely.
        with self._coverage_lock:
            if not self.enabled:
                return default
            try:
                if not self._initialized:
                    self._initialize(create=create)
                    if not self._initialized:
                        return default
                with self._locked() as directory:
                    if self.path.name not in self._check_files(directory):
                        raise _Unavailable("missing")
                    with closing(self._connect()) as database, database:
                        result = operation(database)
                    self.reason = None
                    return result
            except _ERRORS as error:
                self._failure(error)
                return default

    def known_uids(self, folder: str, validity: str) -> set[str] | None:
        """Header coverage evidence, to compare with an authoritative ALL search.

        Counts alone are insufficient: an expunge plus an arrival can leave the
        same count with different UIDs. Snippets never certify full TEXT coverage.
        """

        def query(database: sqlite3.Connection) -> set[str]:
            return {
                row[0]
                for row in database.execute(
                    "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ? "
                    "AND headers_known = 1",
                    (folder, validity),
                )
            }

        return self._run(query)

    def has_mailbox(self, folder: str) -> bool:
        """Whether this cache has any epoch for a mailbox, without an IMAP read."""
        return self._run(
            lambda database: (
                database.execute("SELECT 1 FROM mailboxes WHERE folder = ?", (folder,)).fetchone()
                is not None
            ),
            False,
        )

    def set_all_mail(self, folder: str | None) -> None:
        """Remember the current LIST role, even before its headers are complete.

        An old All Mail folder's coverage remains useful for that mailbox, but
        must stop supplying account counters as soon as LIST moves the role.
        A missing role leaves counters unavailable until a new role is known.
        """

        def update(database: sqlite3.Connection) -> None:
            if folder is None:
                database.execute("DELETE FROM index_roles WHERE role = 'all_mail'")
            else:
                database.execute(
                    "INSERT INTO index_roles VALUES ('all_mail', ?) "
                    "ON CONFLICT(role) DO UPDATE SET folder = excluded.folder "
                    "WHERE index_roles.folder != excluded.folder",
                    (folder,),
                )

        self._run(update)

    def _forget_coverage(self, folder: str, database: sqlite3.Connection) -> None:
        with self._coverage_lock:
            # Retain the inventory summary for local status reporting, but
            # invalidate its proof in the same transaction as the change.
            database.execute("UPDATE coverage SET certified = 0 WHERE folder = ?", (folder,))

    def coverage(self, folder: str) -> tuple[str, int, str] | None:
        """Persisted header proof; callers must validate its epoch against IMAP.

        Every folder uses this same proof, including after a process restart.
        Local mutations invalidate certification atomically with their writes.
        """
        if not self._coverage_lock.acquire(timeout=1.0):
            return None
        try:
            return self._run(lambda database: database.execute(_PROOF, (folder,)).fetchone())
        finally:
            self._coverage_lock.release()

    def recovery_complete(self) -> bool:
        """Local startup gate, never a claim about current live freshness.

        A pending automatic pass must finish its real folders even if All Mail
        is already complete. Legacy and manual caches can use their saved
        proofs without opening Bridge just to rediscover healthy coverage.
        """
        if not self._initialized or not self._coverage_lock.acquire(blocking=False):
            return False
        try:

            def query(database: sqlite3.Connection) -> bool:
                database.execute("BEGIN")
                if database.execute(
                    "SELECT 1 FROM index_roles WHERE role = 'auto_recovery' AND folder = 'pending'"
                ).fetchone():
                    return False
                all_mail = database.execute(
                    "SELECT m.folder FROM index_roles r JOIN mailboxes m ON m.folder = r.folder "
                    "WHERE r.role = 'all_mail'"
                ).fetchone()
                if all_mail is None:
                    return False
                saved = database.execute(
                    "SELECT folder FROM index_roles WHERE role = 'auto_recovery_folders'"
                ).fetchone()
                if saved is None:
                    folders = [row[0] for row in database.execute("SELECT folder FROM mailboxes")]
                else:
                    folders = json.loads(saved[0])
                    if (
                        not isinstance(folders, list)
                        or not folders
                        or any(not isinstance(folder, str) for folder in folders)
                        or all_mail[0] not in folders
                    ):
                        raise _Unavailable("recovery_metadata")
                return not database.execute(
                    "WITH targets(folder) AS (VALUES "
                    + ",".join("(?)" for _ in folders)
                    + ") SELECT 1 FROM targets t LEFT JOIN mailboxes m ON m.folder = t.folder "
                    "LEFT JOIN coverage c ON c.folder = m.folder "
                    "AND c.uidvalidity = m.uidvalidity WHERE c.certified IS NOT 1 "
                    "OR c.message_count != (SELECT count(*) FROM messages h "
                    "WHERE h.folder = m.folder AND h.uidvalidity = m.uidvalidity "
                    "AND h.headers_known = 1) LIMIT 1",
                    folders,
                ).fetchone()

            return self._run(query, False)
        finally:
            self._coverage_lock.release()

    def record_recovery_complete(self, complete: bool, *, folders: list[str] | None = None) -> None:
        """Persist whether a discovered automatic folder pass finished."""

        def update(database: sqlite3.Connection) -> None:
            database.execute(
                "INSERT INTO index_roles VALUES ('auto_recovery', ?) "
                "ON CONFLICT(role) DO UPDATE SET folder = excluded.folder",
                ("complete" if complete else "pending",),
            )
            if folders is not None:
                database.execute(
                    "INSERT INTO index_roles VALUES ('auto_recovery_folders', ?) "
                    "ON CONFLICT(role) DO UPDATE SET folder = excluded.folder",
                    (json.dumps(folders),),
                )

        self._run(update)

    def coverage_epoch(self, folder: str) -> tuple[str, int, str] | None:
        """Last saved inventory epoch, including during a partial catch-up.

        This is a resume checkpoint, never a current header-coverage proof.
        """

        def query(database: sqlite3.Connection) -> tuple[str, int, str] | None:
            return database.execute(
                "SELECT uidvalidity, message_count, uidnext FROM coverage WHERE folder = ?",
                (folder,),
            ).fetchone()

        return self._run(query)

    def record_coverage(
        self,
        folder: str,
        validity: str,
        state: dict[str, Any],
        uids: list[str],
        *,
        known: set[str] | None = None,
        generation: int | None = None,
        account: bool = True,
        catchup: bool = False,
        tail_range: tuple[int, int] | None = None,
    ) -> bool:
        """Remember a complete inventory and its SELECT epoch/count/UIDNEXT.

        An unchanged count and UIDNEXT under the same UIDVALIDITY then prove no
        expunges or arrivals since that inventory, without enumerating it again.
        Callers certify All Mail and LIST-vetted real folders after inventory.
        """
        with self._coverage_lock:
            generation = self.generation if generation is None else generation
            if generation != self.generation:
                return False
            if account:
                self.set_all_mail(folder)
            requested = set(uids)
            # A caller's earlier known set can already be stale after another
            # search reconciles or downgrades headers. Recheck the current DB
            # while writes are excluded; this full read only establishes proofs
            # after an All Mail inventory, never during another folder's STATUS.
            current = self.known_uids(folder, validity)
            uidnext = state.get("uidnext")
            complete = (
                current is not None
                and len(uids) == len(requested) == state["exists"]
                and requested <= current
                and (known is None or requested <= known)
                and isinstance(uidnext, str)
                and uidnext.isascii()
                and uidnext.isdigit()
                and int(uidnext) > max(map(int, uids), default=0)
                and self.enabled
                and self.reason is None
            )
            if complete:

                def save(database: sqlite3.Connection) -> bool:
                    # Recheck under the SQLite transaction too: another server
                    # process can mutate this cache between the earlier reads.
                    database.execute("BEGIN IMMEDIATE")
                    if catchup:
                        if database.execute(
                            "SELECT uidvalidity FROM mailboxes WHERE folder = ?", (folder,)
                        ).fetchone() != (validity,):
                            return False
                        if tail_range is not None:
                            # The bounded tail SEARCH is authoritative only for
                            # this range. A header cached on an earlier partial
                            # attempt may since have been expunged. Reconcile it
                            # atomically with certification and reporting data.
                            removed = [
                                row[0]
                                for row in database.execute(
                                    "SELECT uid FROM messages WHERE folder = ? "
                                    "AND uidvalidity = ? AND CAST(uid AS INTEGER) >= ? "
                                    "AND CAST(uid AS INTEGER) < ?",
                                    (folder, validity, *tail_range),
                                )
                                if row[0] not in requested
                            ]
                            database.executemany(
                                "DELETE FROM messages WHERE folder = ? "
                                "AND uidvalidity = ? AND uid = ?",
                                ((folder, validity, uid) for uid in removed),
                            )
                    saved = {
                        row[0]
                        for row in database.execute(
                            "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ? "
                            "AND headers_known = 1",
                            (folder, validity),
                        )
                    }
                    certified = requested <= saved
                    if catchup:
                        certified = saved == requested
                        if not certified:
                            return False
                    database.execute(
                        "INSERT INTO coverage VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(folder) DO UPDATE SET uidvalidity = excluded.uidvalidity, "
                        "message_count = excluded.message_count, uidnext = excluded.uidnext, "
                        "certified = excluded.certified",
                        (folder, validity, state["exists"], uidnext, int(certified)),
                    )
                    if certified and account:
                        database.execute(
                            "INSERT INTO index_roles VALUES ('all_mail', ?) "
                            "ON CONFLICT(role) DO UPDATE SET folder = excluded.folder",
                            (folder,),
                        )
                        if catchup:
                            self._record_inventory(database, folder, validity, uids, catchup=True)
                    return certified

                complete = self._run(save, False)
            if not complete:
                self._run(lambda database: self._forget_coverage(folder, database))
            return complete

    def cached_headers(
        self, folder: str, validity: str, uids: list[str]
    ) -> dict[str, dict[str, str]]:
        """Known, casefolded decoded search headers for only the requested UIDs.

        These strings support local matching, not display or sender evidence.
        Unknown headers stay absent so callers retain their normal fetch path.
        Use bounded SQL batches within one snapshot for older SQLite limits.
        """
        if not uids:
            return {}

        def query(database: sqlite3.Connection) -> dict[str, dict[str, str]]:
            database.execute("BEGIN")
            result = {}
            for start in range(0, len(uids), 500):
                batch = uids[start : start + 500]
                rows = database.execute(
                    "SELECT uid, from_addr, to_addr, subject FROM messages "
                    "WHERE folder = ? AND uidvalidity = ? AND headers_known = 1 "
                    "AND uid IN (" + ",".join("?" for _ in batch) + ")",
                    [folder, validity, *batch],
                )
                for uid, sender, recipient, subject in rows:
                    result[uid] = {"From": sender, "To": recipient, "Subject": subject}
            return result

        return self._run(query, {})

    def thread_uids(
        self, folder: str, validity: str, state: dict[str, Any], identifiers: list[str]
    ) -> list[str] | None:
        """Related UIDs from complete cached links, or None for the live fallback."""
        if self.coverage(folder) != (validity, state["exists"], state["uidnext"]):
            return None

        def query(database: sqlite3.Connection) -> list[str] | None:
            rows = database.execute(
                "SELECT uid, thread_ids FROM messages WHERE folder = ? AND uidvalidity = ? "
                "AND headers_known = 1",
                (folder, validity),
            ).fetchall()
            # Older caches have search headers but no reply links yet.
            if len(rows) != state["exists"] or any(links is None for _, links in rows):
                return None
            wanted = set(identifiers)
            return [uid for uid, links in rows if wanted.intersection(json.loads(links))]

        return self._run(query)

    def seed_uid(self, folder: str, validity: str, message_id: str) -> str | None:
        """A cheap positive hint that must be confirmed by a live header FETCH.

        Thread seed reads never initialize the cache, wait on its writer/coverage
        locks, or scan a metadata table which predates the Message-ID index.
        """
        if not self.enabled:
            return None
        directory = None
        try:
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _private(os.fstat(directory), directory=True)
            # A read-only WAL connection can create a missing shared-memory
            # sidecar. Decline active journals instead of changing cache files.
            if self._check_files(directory) != {self.path.name}:
                return None
            with closing(
                sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0)
            ) as database:
                database.set_progress_handler(lambda: 1, 1000)
                if database.execute("PRAGMA user_version").fetchone() != (_VERSION,):
                    return None
                row = database.execute(
                    "SELECT uid FROM search_metadata INDEXED BY search_metadata_message_id "
                    "WHERE folder = ? AND uidvalidity = ? AND message_id = ? LIMIT 1",
                    (folder, validity, message_id),
                ).fetchone()
                uid = row[0] if row else None
                return (
                    uid
                    if isinstance(uid, str) and uid.isascii() and uid.isdigit() and int(uid) > 0
                    else None
                )
        except _ERRORS:
            return None
        finally:
            if directory is not None:
                os.close(directory)

    def metadata_uids(self, folder: str, validity: str) -> set[str]:
        """UIDs with dates and attempted IDs, including known absent/invalid IDs."""

        def query(database: sqlite3.Connection) -> set[str]:
            return {
                row[0]
                for row in database.execute(
                    "SELECT uid FROM search_metadata WHERE folder = ? AND uidvalidity = ? "
                    "AND internaldate IS NOT NULL "
                    "AND (identifier_known = 1 OR message_id IS NOT NULL)",
                    (folder, validity),
                )
            }

        return self._run(query, set())

    def cached_metadata(
        self, folder: str, validity: str, uids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Cached immutable dates and optional Message-IDs for requested UIDs."""
        if not uids:
            return {}

        def query(database: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            database.execute("BEGIN")
            result = {}
            for start in range(0, len(uids), 500):
                batch = uids[start : start + 500]
                for uid, stamp, identifier in database.execute(
                    "SELECT uid, internaldate, message_id FROM search_metadata "
                    "WHERE folder = ? AND uidvalidity = ? AND internaldate IS NOT NULL "
                    "AND uid IN (" + ",".join("?" for _ in batch) + ")",
                    [folder, validity, *batch],
                ):
                    result[uid] = {"internaldate": stamp, "message_id": identifier}
            return result

        return self._run(query, {})

    def ordered_page(
        self,
        folder: str,
        validity: str,
        uids: list[str],
        conditions: list[tuple],
        limit: int,
        last: tuple[float, int] | None = None,
    ) -> tuple[int, list[tuple[str, float]]] | None:
        """Count header matches and select only one chronological page locally.

        The caller supplies the live, date-scoped candidate inventory, bounded
        by the cursor's original maximum UID. Every candidate must have complete
        cached headers and an immutable INTERNALDATE, or the caller keeps its
        ordinary fetch path. One extra row reports whether another page exists.
        """

        def predicate(node: tuple, parameters: list[Any]) -> str | None:
            kind, value = node
            if kind == "not":
                child = predicate(value, parameters)
                return None if child is None else "NOT (" + child + ")"
            if kind in {"and", "or"}:
                children = [predicate(child, parameters) for child in value]
                if any(child is None for child in children):
                    return None
                return "(" + (" AND " if kind == "and" else " OR ").join(children) + ")"
            if kind not in _COLUMNS or "\x00" in value:
                return None
            parameters.append(value.casefold())
            return "instr(m." + _COLUMNS[kind] + ", ?) > 0"

        parameters: list[Any] = []
        clauses = [predicate(node, parameters) for node in conditions]
        if any(clause is None for clause in clauses):
            return None

        def query(database: sqlite3.Connection) -> tuple[int, list[tuple[str, float]]] | None:
            database.execute("BEGIN")
            # A temporary inventory avoids SQLite's variable limit and never
            # downloads headers/dates for the entire match set into Python.
            database.execute("CREATE TEMP TABLE page_candidates (uid TEXT PRIMARY KEY)")
            database.executemany(
                "INSERT OR IGNORE INTO page_candidates VALUES (?)", ((uid,) for uid in uids)
            )
            source = (
                " FROM page_candidates p LEFT JOIN messages m ON m.uid = p.uid "
                "AND m.folder = ? AND m.uidvalidity = ? "
                "LEFT JOIN search_metadata d ON d.uid = m.uid AND d.folder = m.folder "
                "AND d.uidvalidity = m.uidvalidity"
            )
            if database.execute(
                "SELECT 1" + source + " WHERE m.headers_known IS NOT 1 "
                "OR d.internaldate IS NULL LIMIT 1",
                (folder, validity),
            ).fetchone():
                return None
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            arguments = [folder, validity, *parameters]
            total = database.execute("SELECT count(*)" + source + where, arguments).fetchone()[0]
            if last is not None:
                where += (" AND " if where else " WHERE ") + (
                    "(d.internaldate < ? OR (d.internaldate = ? AND d.uid_number < ?))"
                )
                arguments.extend((last[0], last[0], last[1]))
            rows = database.execute(
                "SELECT m.uid, d.internaldate"
                + source
                + where
                + " ORDER BY d.internaldate DESC, d.uid_number DESC LIMIT ?",
                [*arguments, limit + 1],
            ).fetchall()
            return total, rows

        return self._run(query)

    def map_headers(
        self,
        source_folder: str,
        source_validity: str,
        target_folder: str,
        target_validity: str,
        metadata: dict[str, dict[str, Any]],
    ) -> set[str]:
        """Reuse unique All Mail search headers for a folder's own live UIDs.

        ``metadata`` must contain the full target inventory, fetched cheaply
        with ENVELOPE and INTERNALDATE. Exact unique Message-IDs support only
        header filtering: they never establish canonical action identities.
        Missing and duplicate IDs remain unknown and retain the fetch fallback.
        """
        if not metadata:
            return set()
        self.observe(target_folder, target_validity, metadata, membership=True)

        def update(database: sqlite3.Connection) -> set[str]:
            database.execute("BEGIN IMMEDIATE")
            database.execute("CREATE TEMP TABLE mapped_candidates (uid TEXT PRIMARY KEY)")
            database.executemany(
                "INSERT OR IGNORE INTO mapped_candidates VALUES (?)", ((uid,) for uid in metadata)
            )
            source = (
                " FROM search_metadata d JOIN messages s ON s.folder = d.folder "
                "AND s.uidvalidity = d.uidvalidity AND s.uid = d.uid "
                "JOIN search_metadata t ON t.message_id = d.message_id "
                "JOIN mapped_candidates p ON p.uid = t.uid "
                "WHERE d.folder = ? AND d.uidvalidity = ? AND s.headers_known = 1 "
                "AND t.folder = ? AND t.uidvalidity = ? AND d.message_id IS NOT NULL "
                "AND (d.envelope_digest IS NULL OR t.envelope_digest IS NULL "
                "OR d.envelope_digest = t.envelope_digest) "
                "AND (SELECT count(*) FROM search_metadata a WHERE a.folder = d.folder "
                "AND a.uidvalidity = d.uidvalidity AND a.message_id = d.message_id) = 1 "
                "AND (SELECT count(*) FROM search_metadata b "
                "WHERE b.folder = t.folder AND b.uidvalidity = t.uidvalidity "
                "AND b.message_id = t.message_id) = 1"
            )
            rows = database.execute(
                "SELECT t.uid, s.subject, s.from_addr, s.to_addr, s.cc" + source,
                (source_folder, source_validity, target_folder, target_validity),
            ).fetchall()
            database.executemany(
                "UPDATE messages SET subject = ?, from_addr = ?, to_addr = ?, cc = ?, "
                "headers_known = 1 WHERE folder = ? AND uidvalidity = ? AND uid = ? "
                "AND headers_known = 0",
                (
                    (subject, sender, recipient, cc, target_folder, target_validity, uid)
                    for uid, subject, sender, recipient, cc in rows
                ),
            )
            return {row[0] for row in rows}

        return self._run(update, set())

    def indexed_locations(
        self,
        handles: list[dict[str, Any]],
        real: set[str],
        all_mail: str | None,
        epochs: dict[str, tuple[str, int, str]],
    ) -> list[dict[str, str] | Unplaced | None] | None:
        """Canonical All Mail locations from certified fingerprints, in one snapshot.

        ``epochs`` holds All Mail and the real folders the caller matched with live
        STATUS; it verifies them again after this read. All Mail lists every message
        once, so it needs complete fingerprints, and its single copy makes a known
        row in a proven folder the message itself. Other folders need no proof, and
        their rows may be stale, so they only name the likely ``holders``.

        Each handle gets its location, None when All Mail or the proven folders show
        several copies, or ``Unplaced`` when no proven folder has a row. The whole
        result is None without All Mail's proof or a handle's fingerprint.
        """
        if (
            all_mail is None
            or all_mail not in epochs
            or any(item.get("folder") != all_mail for item in handles)
        ):
            return None

        def query(database: sqlite3.Connection) -> list[dict[str, str] | Unplaced | None] | None:
            database.execute("BEGIN")
            for folder, epoch in epochs.items():
                if database.execute(_PROOF, (folder,)).fetchone() != epoch:
                    return None
            counts = database.execute(
                "SELECT count(*), coalesce(sum(CASE WHEN m.headers_known = 1 "
                "AND d.internaldate IS NOT NULL AND d.envelope_digest IS NOT NULL "
                "AND d.size IS NOT NULL THEN 1 ELSE 0 END), 0) "
                "FROM messages m LEFT JOIN search_metadata d "
                "ON d.folder = m.folder AND d.uidvalidity = m.uidvalidity AND d.uid = m.uid "
                "WHERE m.folder = ? AND m.uidvalidity = ?",
                (all_mail, epochs[all_mail][0]),
            ).fetchone()
            if counts != (epochs[all_mail][1], epochs[all_mail][1]):
                return None
            database.execute("CREATE TEMP TABLE canonical_folders (folder TEXT PRIMARY KEY)")
            database.executemany(
                "INSERT INTO canonical_folders VALUES (?)", ((folder,) for folder in real)
            )
            result: list[dict[str, str] | Unplaced | None] = []
            for item in handles:
                if item.get("uidvalidity") != epochs[all_mail][0]:
                    return None
                fingerprint = database.execute(
                    "SELECT envelope_digest, internaldate, size FROM search_metadata "
                    "WHERE folder = ? AND uidvalidity = ? AND uid = ?",
                    (all_mail, item["uidvalidity"], item["uid"]),
                ).fetchone()
                if fingerprint is None or any(value is None for value in fingerprint):
                    return None
                copies = database.execute(
                    "SELECT count(*) FROM search_metadata WHERE folder = ? AND uidvalidity = ? "
                    "AND envelope_digest = ? AND internaldate = ? AND size = ?",
                    (all_mail, epochs[all_mail][0], *fingerprint),
                ).fetchone()[0]
                rows = database.execute(
                    "SELECT d.folder, d.uidvalidity, d.uid FROM search_metadata d "
                    "JOIN canonical_folders f ON f.folder = d.folder "
                    "JOIN mailboxes b ON b.folder = d.folder AND b.uidvalidity = d.uidvalidity "
                    "JOIN messages m ON m.folder = d.folder AND m.uidvalidity = d.uidvalidity "
                    "AND m.uid = d.uid WHERE m.headers_known = 1 AND d.envelope_digest = ? "
                    "AND d.internaldate = ? AND d.size = ?",
                    fingerprint,
                ).fetchall()
                proven = [row for row in rows if row[0] in epochs and row[1] == epochs[row[0]][0]]
                if copies != 1 or len(proven) > 1:
                    result.append(None)
                elif proven:
                    result.append(dict(zip(("folder", "uidvalidity", "uid"), proven[0])))
                else:
                    result.append(Unplaced(fingerprint, tuple(sorted({row[0] for row in rows}))))
            return result

        return self._run(query)

    def may_hold_unfingerprinted(self, folder: str, validity: str, stamps: set[float]) -> bool:
        """Whether a proven folder has a row its fingerprints cannot rule out for these dates.

        A row cached before plain pages fetched the envelope and size, or by the
        filter-search fallback, has no fingerprint, so it can be a wanted message
        that the fingerprint lookup did not find.
        """
        marks = ",".join("?" for _ in stamps)

        def query(database: sqlite3.Connection) -> bool:
            return (
                database.execute(
                    "SELECT 1 FROM messages m LEFT JOIN search_metadata d ON d.folder = m.folder "
                    "AND d.uidvalidity = m.uidvalidity AND d.uid = m.uid "
                    "WHERE m.folder = ? AND m.uidvalidity = ? AND m.headers_known = 1 "
                    "AND (d.internaldate IS NULL OR (d.internaldate IN (" + marks + ") "
                    "AND (d.envelope_digest IS NULL OR d.size IS NULL))) LIMIT 1",
                    (folder, validity, *stamps),
                ).fetchone()
                is not None
            )

        return self._run(query, True)

    def covers(self, folder: str, validity: str, uids: list[str]) -> bool:
        """Whether every supplied live UID has known headers in one snapshot.

        Callers must supply a full live inventory to certify folder coverage.
        Bounded batches avoid SQLite's parameter limit without loading headers.
        """

        def query(database: sqlite3.Connection) -> bool:
            database.execute("BEGIN")
            requested = list(set(uids))
            for start in range(0, len(requested), 500):
                batch = requested[start : start + 500]
                count = database.execute(
                    "SELECT count(*) FROM messages WHERE folder = ? AND uidvalidity = ? "
                    "AND headers_known = 1 AND uid IN (" + ",".join("?" for _ in batch) + ")",
                    [folder, validity, *batch],
                ).fetchone()[0]
                if count != len(batch):
                    return False
            return True

        return self._run(query, False)

    def known_count(
        self, folder: str, validity: str, *, uids: list[str] | None = None
    ) -> int | None:
        """Count complete headers, optionally restricted to one fetched batch."""
        if uids == []:
            return 0
        statement = (
            "SELECT count(*) FROM messages WHERE folder = ? AND uidvalidity = ? "
            "AND headers_known = 1"
        )
        parameters = [folder, validity]
        if uids is not None:
            statement += " AND uid IN (" + ",".join("?" for _ in uids) + ")"
            parameters.extend(uids)
        return self._run(lambda database: database.execute(statement, parameters).fetchone()[0])

    def reporting_counts(self) -> dict[str, int] | None:
        """Local All Mail counts for status, without certifying live freshness.

        Coverage metadata remembers the last inventory even when its proof has
        been invalidated. Count only complete headers in the current epoch; do
        not sum mailbox rows, since labels can duplicate All Mail messages.
        The remembered All Mail role avoids confusing a newer real-folder proof
        with an older All Mail inventory. These local counts may include
        rows expunged since the last authoritative inventory was reconciled.
        """
        if not self._coverage_lock.acquire(blocking=False):
            return None
        try:

            def query(database: sqlite3.Connection) -> dict[str, int] | None:
                row = database.execute(
                    "SELECT c.message_count, count(m.uid) FROM coverage c "
                    "JOIN mailboxes b ON b.folder = c.folder AND b.uidvalidity = c.uidvalidity "
                    "LEFT JOIN messages m ON m.folder = b.folder "
                    "AND m.uidvalidity = b.uidvalidity AND m.headers_known = 1 "
                    "WHERE c.folder = (SELECT folder FROM index_roles WHERE role = 'all_mail') "
                    "GROUP BY c.folder"
                ).fetchone()
                if row is None:
                    return None
                return {"all_mail_count": row[0], "indexed_count": row[1]}

            return self._run(query)
        finally:
            self._coverage_lock.release()

    def reporting_count(self, folder: str, validity: str) -> int | None:
        """Snapshot known headers for a read's epoch without waiting or initializing."""
        if not self._initialized or not self._coverage_lock.acquire(blocking=False):
            return None
        try:
            return self.known_count(folder, validity)
        finally:
            self._coverage_lock.release()

    def reporting_status(self) -> dict[str, Any] | None:
        """Current All Mail headers and its last observed inventory, with no IMAP I/O."""
        if not self._coverage_lock.acquire(blocking=False):
            return None
        try:

            def query(database: sqlite3.Connection) -> dict[str, Any] | None:
                database.execute("BEGIN")
                mailbox = database.execute(
                    "SELECT m.folder, m.uidvalidity FROM mailboxes m JOIN index_roles r "
                    "ON r.folder = m.folder WHERE r.role = 'all_mail'"
                ).fetchone()
                if mailbox is None:
                    return None
                count, maximum = database.execute(
                    "SELECT count(*), coalesce(max(CAST(uid AS INTEGER)), 0) FROM messages "
                    "WHERE folder = ? AND uidvalidity = ? AND headers_known = 1",
                    mailbox,
                ).fetchone()
                result = {
                    "indexed_count": count,
                    "indexed_max_uid": str(maximum),
                    "all_mail_count": None,
                    "all_mail_max_uid": None,
                    "all_mail_observed_at": None,
                    "last_catchup_at": None,
                    "last_expunge_reconciliation_at": None,
                }
                inventory = database.execute(
                    "SELECT message_count, max_uid, observed_at, last_catchup_at, "
                    "NULLIF(last_expunge_reconciliation_at, '') FROM index_sync "
                    "WHERE folder = ? AND uidvalidity = ?",
                    mailbox,
                ).fetchone()
                if inventory is not None:
                    result.update(
                        zip(
                            (
                                "all_mail_count",
                                "all_mail_max_uid",
                                "all_mail_observed_at",
                                "last_catchup_at",
                                "last_expunge_reconciliation_at",
                            ),
                            inventory,
                        )
                    )
                return result

            return self._run(query)
        finally:
            self._coverage_lock.release()

    @staticmethod
    def _record_inventory(
        database: sqlite3.Connection,
        folder: str,
        validity: str,
        uids: list[str],
        *,
        catchup: bool = False,
    ) -> None:
        # Reporting metadata never certifies header coverage.
        now = datetime.now(UTC).isoformat()
        database.execute(
            "INSERT INTO index_sync VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(folder) DO UPDATE SET uidvalidity = excluded.uidvalidity, "
            "message_count = excluded.message_count, max_uid = excluded.max_uid, "
            "observed_at = excluded.observed_at, last_catchup_at = "
            "CASE WHEN excluded.last_catchup_at IS NOT NULL THEN excluded.last_catchup_at "
            "WHEN index_sync.uidvalidity = excluded.uidvalidity "
            "THEN index_sync.last_catchup_at ELSE NULL END, "
            "last_expunge_reconciliation_at = CASE WHEN excluded.last_catchup_at IS NOT NULL "
            "THEN index_sync.last_expunge_reconciliation_at "
            "ELSE excluded.last_expunge_reconciliation_at END",
            (
                folder,
                validity,
                len(set(uids)),
                str(max(map(int, uids), default=0)),
                now,
                now if catchup else None,
                # An empty value means no ALL reconciliation has been observed;
                # reporting exposes None.
                "" if catchup else now,
            ),
        )

    def record_catchup(self, folder: str, validity: str, *, generation: int) -> None:
        """Remember a successful incremental sync, separate from full rebuilds."""

        def update(database: sqlite3.Connection) -> None:
            database.execute(
                "UPDATE index_sync SET last_catchup_at = ? WHERE folder = ? AND uidvalidity = ?",
                (datetime.now(UTC).isoformat(), folder, validity),
            )

        with self._coverage_lock:
            if generation == self.generation:
                self._run(update)

    def create_candidate(self, settings: Any, run_id: str) -> SearchIndex:
        """Create an independent private database alongside the live cache."""
        if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
            raise ValueError("invalid_run_id")
        path = self.path.with_name(self.path.name + ".reindex-" + run_id)
        # Never reset the live object or open it through the candidate's path.
        candidate = SearchIndex(settings, _path=path, _candidate=True)
        if path.exists():
            raise _Unavailable("candidate_exists")
        if candidate.enabled:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with candidate._locked(exclusive=True, create=True, retain=True):
                    if path.exists():
                        raise _Unavailable("candidate_exists")
                    candidate._initialize(create=True)
            except _ERRORS as error:
                candidate.release_candidate()
                candidate._failure(error)
        return candidate

    def acquire_reindex_guard(self) -> int:
        """An account run lease spanning the start audit through its outcome."""
        if self.reason == "repository_path":
            raise _Unavailable("repository_path")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptor = None
        try:
            _private(os.fstat(directory), directory=True)
            descriptor = os.open(
                self.path.name + ".reindex.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            _private(os.fstat(descriptor))
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise
        finally:
            os.close(directory)

    def sweep_candidates(self) -> tuple[set[str], set[str]]:
        """Reclaim this account's exact candidate names, skipping held locks."""
        removed, busy = set(), set()
        try:
            names = os.listdir(self.path.parent)
        except OSError:
            return removed, busy
        prefix = self.path.name + ".reindex-"
        run_ids = {
            name[len(prefix) : len(prefix) + 32]
            for name in names
            if name.startswith(prefix)
            and len(name[len(prefix) : len(prefix) + 32]) == 32
            and all(char in "0123456789abcdef" for char in name[len(prefix) : len(prefix) + 32])
            and name[len(prefix) + 32 :] in {"", ".lock", "-journal", "-wal", "-shm"}
        }
        for run_id in run_ids:
            candidate = object.__new__(SearchIndex)
            candidate.path = self.path.with_name(prefix + run_id)
            candidate._candidate_lock = None
            try:
                candidate.discard(create_lock=True)
                removed.add(run_id)
            except BlockingIOError:
                busy.add(run_id)
            except _ERRORS:
                # Unsafe or unavailable files are left untouched, with no
                # message data or filesystem paths disclosed in logs.
                busy.add(run_id)
        return removed, busy

    def replace_with(self, candidate: SearchIndex) -> None:
        """Install one fully closed candidate while every live reader is excluded.

        All normal operations open and close SQLite under the same live lock.
        The next operation therefore opens either the complete old file or the
        complete new file. Refuse unexpected SQLite sidecars instead of risking
        attaching a live journal or WAL to the replacement database.
        """
        if (
            candidate.path.parent != self.path.parent
            or candidate.path == self.path
            or not candidate.enabled
            or not candidate._initialized
            or candidate.reason is not None
        ):
            raise _Unavailable("candidate_unavailable")
        with self._coverage_lock, candidate._coverage_lock:
            with candidate._locked(exclusive=True) as source:
                if candidate._check_files(source) != {candidate.path.name}:
                    raise _Unavailable("candidate_sidecars")
                with self._locked(exclusive=True, create=True) as target:
                    if self._check_files(target) - {self.path.name}:
                        raise _Unavailable("live_sidecars")
                    os.replace(
                        candidate.path.name, self.path.name, src_dir_fd=source, dst_dir_fd=target
                    )
                    self.enabled = self._initialized = True
                    self.reason = None
                    self.generation += 1
                    candidate._initialized = False

    def discard(self, *, create_lock: bool = False) -> None:
        """Remove only this private candidate's files after its worker stops."""
        try:
            with self._locked(exclusive=True, create=create_lock) as directory:
                for name in self._check_files(directory):
                    os.unlink(name, dir_fd=directory)
                os.unlink(self.path.name + ".lock", dir_fd=directory)
            self.enabled = self._initialized = False
        finally:
            self.release_candidate()

    def release_candidate(self) -> None:
        """Release a worker lease even when removing candidate files failed."""
        if self._candidate_lock is not None:
            os.close(self._candidate_lock)
            self._candidate_lock = None

    def _select(self, database: sqlite3.Connection, folder: str, validity: str) -> None:
        previous = database.execute(
            "SELECT uidvalidity FROM mailboxes WHERE folder = ?", (folder,)
        ).fetchone()
        if previous == (validity,):
            return
        self._forget_coverage(folder, database)
        database.execute("DELETE FROM messages WHERE folder = ?", (folder,))
        database.execute(
            "INSERT INTO mailboxes VALUES (?, ?) ON CONFLICT(folder) DO UPDATE "
            "SET uidvalidity = excluded.uidvalidity",
            (folder, validity),
        )

    def select(self, folder: str, validity: str) -> None:
        """Invalidate the old folder epoch before accepting reused UIDs."""
        self._run(lambda database: self._select(database, folder, validity))

    def observe(
        self,
        folder: str,
        validity: str,
        metadata: dict[str, dict[str, Any]],
        *,
        membership: bool = False,
    ) -> None:
        """Upsert fetched headers and enrich their immutable cheap metadata.

        Ordinary metadata reads do not create unknown message rows for
        browsed label views. Explicit membership warming retains those rows so
        bounded batches can be mapped after the full inventory is available.
        """
        if not self.enabled:
            return
        try:
            has_headers = any(item.get("headers") is not None for item in metadata.values())
            if not has_headers and not any(
                item.get("internaldate") is not None or item.get("envelope") is not None
                for item in metadata.values()
            ):
                return
        except _ERRORS as error:
            self._failure(error)
            return

        def update(database: sqlite3.Connection) -> None:
            rows = []
            metadata_rows = []
            retained = set()
            if not membership:
                requested = [
                    uid
                    for uid, item in metadata.items()
                    if isinstance(uid, str) and item.get("headers") is None
                ]
                for start in range(0, len(requested), 500):
                    batch = requested[start : start + 500]
                    retained.update(
                        row[0]
                        for row in database.execute(
                            "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ? "
                            "AND uid IN (" + ",".join("?" for _ in batch) + ")",
                            [folder, validity, *batch],
                        )
                    )
            for uid, item in metadata.items():
                if (
                    not isinstance(uid, str)
                    or not uid.isascii()
                    or not uid.isdigit()
                    or int(uid) < 1
                ):
                    continue
                headers = item.get("headers")
                if headers is None and not membership and uid not in retained:
                    continue
                envelope = item.get("envelope")
                has_identifier = headers is not None or (
                    isinstance(envelope, list) and len(envelope) == 10
                )
                identifier = None
                if has_identifier:
                    from .mail import MailValidationError, _message_id

                    try:
                        if headers is not None:
                            values = (
                                [
                                    value
                                    for name, value in headers.raw_items()
                                    if name.casefold() == "message-id"
                                ]
                                if hasattr(headers, "raw_items")
                                else [headers.get("Message-ID", "")]
                            )
                            value = (
                                str(values[0])
                                .encode("utf-8", "surrogateescape")
                                .decode("utf-8", "strict")
                                if len(values) == 1
                                else ""
                            )
                        else:
                            value = envelope[9]
                            if isinstance(value, bytes):
                                value = value.decode("utf-8", "strict")
                        identifier = _message_id(value.strip())
                    except (MailValidationError, UnicodeError, AttributeError, TypeError):
                        identifier = None
                stamp = item.get("internaldate")
                if type(stamp) not in (int, float) or not math.isfinite(stamp):
                    stamp = None
                digest = (
                    hashlib.sha256(repr(envelope).encode("ascii", "backslashreplace")).hexdigest()
                    if isinstance(envelope, list) and len(envelope) == 10
                    else None
                )
                size = item.get("size")
                if type(size) is not int or size < 0:
                    size = None
                if stamp is not None or has_identifier or size is not None:
                    metadata_rows.append(
                        (
                            folder,
                            validity,
                            uid,
                            int(uid),
                            stamp,
                            identifier,
                            digest,
                            size,
                            int(has_identifier),
                        )
                    )
                if headers is None:
                    continue
                from .mail import _ids

                thread_ids = json.dumps(
                    ([identifier] if identifier is not None else [])
                    + _ids(str(headers.get("References", "")))
                    + _ids(str(headers.get("In-Reply-To", "")))
                )
                sender = headers.get("From")
                if sender is None:
                    sender = ""
                else:
                    try:
                        sender = str(_parsed_sender(sender))
                    except (ValueError, TypeError, AttributeError, IndexError):
                        sender = str(sender)
                fields = [
                    str(headers.get("Subject", "")),
                    sender,
                    str(headers.get("To", "")),
                    str(headers.get("Cc", "")),
                ]
                # SQLite's string functions stop at NUL. Keep such headers unknown
                # rather than pruning a substring after that byte.
                known = int(not any("\x00" in value for value in fields))
                if not known:
                    self._forget_coverage(folder, database)
                rows.append(
                    (
                        folder,
                        validity,
                        uid,
                        *(value.casefold() for value in fields),
                        known,
                        thread_ids,
                    )
                )
            if not rows and not metadata_rows:
                return
            self._select(database, folder, validity)
            if rows:
                database.executemany(
                    "INSERT INTO messages(folder, uidvalidity, uid, subject, from_addr, "
                    "to_addr, cc, headers_known, thread_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(folder, uidvalidity, uid) DO UPDATE SET "
                    "subject = excluded.subject, from_addr = excluded.from_addr, "
                    "to_addr = excluded.to_addr, cc = excluded.cc, "
                    "headers_known = excluded.headers_known, thread_ids = excluded.thread_ids "
                    "WHERE (messages.subject, messages.from_addr, messages.to_addr, messages.cc, "
                    "messages.headers_known) != (excluded.subject, excluded.from_addr, "
                    "excluded.to_addr, excluded.cc, excluded.headers_known) "
                    "OR messages.thread_ids IS NOT excluded.thread_ids",
                    rows,
                )
            if metadata_rows:
                database.executemany(
                    "INSERT INTO messages(folder, uidvalidity, uid) VALUES (?, ?, ?) "
                    "ON CONFLICT(folder, uidvalidity, uid) DO NOTHING",
                    (row[:3] for row in metadata_rows),
                )
                database.executemany(
                    "INSERT INTO search_metadata(folder, uidvalidity, uid, uid_number, "
                    "internaldate, message_id, envelope_digest, size, identifier_known) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(folder, uidvalidity, uid) DO UPDATE SET "
                    "internaldate = coalesce(excluded.internaldate, search_metadata.internaldate), "
                    "envelope_digest = coalesce(excluded.envelope_digest, "
                    "search_metadata.envelope_digest), "
                    "size = coalesce(excluded.size, search_metadata.size), "
                    "identifier_known = max(search_metadata.identifier_known, "
                    "excluded.identifier_known), "
                    "message_id = CASE WHEN excluded.identifier_known THEN excluded.message_id "
                    "ELSE search_metadata.message_id END",
                    metadata_rows,
                )

        self._run(update, create=membership or has_headers)

    def remove(self, folder: str, validity: str, uids: list[str]) -> None:
        """Forget only the supplied identities following a confirmed removal."""

        def update(database: sqlite3.Connection) -> None:
            self._forget_coverage(folder, database)
            database.executemany(
                "DELETE FROM messages WHERE folder = ? AND uidvalidity = ? AND uid = ?",
                ((folder, validity, uid) for uid in uids),
            )

        self._run(update)

    def reconcile(
        self, folder: str, validity: str, uids: list[str], *, state: dict[str, Any] | None = None
    ) -> None:
        """Remove expunged messages using an authoritative unfiltered UID inventory.

        ``state`` is the SELECT this inventory was read under. Removal clears the
        folder's proof, and the same inventory restores it when ``record_coverage``
        finds every live UID known, so one inventory never leaves a consistent
        folder uncertified. Holding the lock keeps other readers from seeing the gap.
        """

        def update(database: sqlite3.Connection) -> None:
            self._forget_coverage(folder, database)
            self._select(database, folder, validity)
            present = set(uids)
            removed = [
                row[0]
                for row in database.execute(
                    "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ?",
                    (folder, validity),
                )
                if row[0] not in present
            ]
            database.executemany(
                "DELETE FROM messages WHERE folder = ? AND uidvalidity = ? AND uid = ?",
                ((folder, validity, uid) for uid in removed),
            )
            self._record_inventory(database, folder, validity, uids)

        with self._coverage_lock:
            self._run(update)
            if state is not None:
                self.record_coverage(folder, validity, state, uids, account=False)

    def candidates(
        self, folder: str, validity: str, uids: list[str], nodes: list[tuple]
    ) -> list[str]:
        """Match all known headers before callers cap expensive uncached work.

        Each node returns possible-true and possible-false sets. Swapping those
        sets for NOT keeps an unknown child unknown, including under nested OR.
        Stored headers and query values are casefolded, so instr implements the
        exact decoded substring semantics, including short terms and literal
        SQL wildcard characters. Unknown headers and TEXT remain candidates.
        """
        if not uids or not nodes:
            return uids

        def query(database: sqlite3.Connection) -> list[str]:
            database.execute("BEGIN")
            universe = set(uids)
            known = universe.intersection(
                row[0]
                for row in database.execute(
                    "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ? "
                    "AND headers_known = 1",
                    (folder, validity),
                )
            )
            unknown = universe - known
            cached: dict[tuple, tuple[set[str], set[str]]] = {}

            def possible(node: tuple) -> tuple[set[str], set[str]]:
                kind, value = node
                if kind == "not":
                    yes, no = possible(value)
                    return no, yes
                if kind in {"and", "or"}:
                    children = [possible(child) for child in value]
                    if kind == "and":
                        return set.intersection(*(yes for yes, _ in children)), set.union(
                            *(no for _, no in children)
                        )
                    return set.union(*(yes for yes, _ in children)), set.intersection(
                        *(no for _, no in children)
                    )
                if kind not in _COLUMNS or "\x00" in value:
                    return universe, universe
                if node not in cached:
                    hits = known.intersection(
                        row[0]
                        for row in database.execute(
                            "SELECT uid FROM messages WHERE folder = ? AND uidvalidity = ? "
                            "AND headers_known = 1 AND instr(" + _COLUMNS[kind] + ", ?) > 0",
                            (folder, validity, value.casefold()),
                        )
                    )
                    cached[node] = hits | unknown, universe - hits
                return cached[node]

            selected = set.intersection(*(possible(node)[0] for node in nodes))
            return [uid for uid in uids if uid in selected]

        return self._run(query, uids)
