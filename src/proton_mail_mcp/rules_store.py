"""Saved custom folder rules: a private local store that only ever grows by one reviewed rule.

Each saved rule is one validated Sieve filter model (sieve.filter_spec), kept in its
canonical form in a JSON file beside the action journal (PROTON_MCP_SAVED_RULES
overrides the path), apart from the packaged folder_rules.json defaults. The rules
engine (folder_rules) applies saved rules over IMAP after the packaged ones.
LocalRulesBackend is add_rule's default apply backend behind the same interface as
a Sieve backend: it appends exactly the reviewed rule and never replaces, edits,
reorders or removes one. An unreadable or invalid store refuses (fails closed).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .action_log import ActionLog
from .config import ConfigError
from .mail import MailValidationError
from .sieve import SieveFilter, filter_spec, parse_filter

BACKEND = "local_rules"
VERSION = 1
# As in folder_rules.json: every rule costs one Bridge search per rules preview.
MAX_SAVED_RULES = 20
# Room for twenty rules of ten 500-character conditions, even fully \u-escaped.
MAX_STORE_BYTES = 2 * 1024 * 1024
_RULE_KEYS = frozenset({"name", "folder", "conditions", "match", "mark_read"})
_INVALID = "Cannot load valid saved rules; fix or move the saved rules store"
_CHANGED = "Saved rules store changed after preview; preview again"
# One step down a resolved directory: a real directory, never a link. O_PATH
# (Linux) needs only search permission on the way, as path lookup does.
_STEP = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SavedRulesError(ConfigError, MailValidationError):
    """An invalid store is both invalid configuration and a refused rules call."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def store_path(settings: Any) -> Path:
    """PROTON_MCP_SAVED_RULES, otherwise saved-rules.json beside the action journal."""
    configured = getattr(settings, "saved_rules_path", None)
    if configured:
        return Path(configured)
    journal = ActionLog(getattr(settings, "action_log_path", None)).path
    return journal.with_name("saved-rules.json")


def rule_sha256(spec: SieveFilter) -> str:
    """Digest of one rule's canonical stored form."""
    payload = json.dumps(spec.params(), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def name_key(name: str) -> str:
    """Rule names that are equal after compatibility folding and casefolding conflict."""
    return unicodedata.normalize("NFKC", name).casefold()


def _owned(descriptor: int, what: str) -> os.stat_result:
    """This user's regular file without other links, checked before it is trusted or changed."""
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise SavedRulesError(f"{what} must be a private regular file (mode 0600)")
    return info


def _private(descriptor: int, what: str) -> None:
    if _owned(descriptor, what).st_mode & 0o077:
        raise SavedRulesError(f"{what} must be a private regular file (mode 0600)")


def _resolve(path: Path) -> tuple[str, str]:
    """The store's real directory (links and .. resolved) and its own file name.

    Only the directory resolves: the store, its lock and temporary files are
    opened in it by name, and a link as the store itself is refused.
    """
    if path.name in {"", ".", ".."}:
        raise SavedRulesError("The saved rules store path must name a file")
    try:
        return os.path.realpath(path.parent), path.name
    except (OSError, ValueError):
        raise SavedRulesError(_INVALID) from None


def _descend(directory: int, parts: Sequence[str], *, create: bool = False) -> int:
    """Open a directory below an open one, one component of parts at a time, never via a link.

    The descriptor is then the directory this path names: a link (even one that
    appeared after resolution), a file or a missing component raises. create
    makes missing directories as mkdir -p does, only the last one private.
    """
    descriptor = directory
    try:
        for depth, part in enumerate(parts, start=1):
            try:
                step = os.open(part, _STEP, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700 if depth == len(parts) else 0o777, dir_fd=descriptor)
                step = os.open(part, _STEP, dir_fd=descriptor)
            if descriptor != directory:
                os.close(descriptor)
            descriptor = step
        # Readable, unlike an O_PATH step: each save fsyncs the directory.
        return os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=descriptor)
    finally:
        if descriptor != directory:
            os.close(descriptor)


def _open_directory(directory: str, *, create: bool = False) -> int:
    """Open a resolved directory from the root down, as _descend does."""
    root, *parts = Path(directory).parts
    descriptor = os.open(root, _STEP)
    try:
        return _descend(descriptor, parts, create=create)
    finally:
        os.close(descriptor)


def _existing(directory: str) -> list[int]:
    """Each existing directory on a resolved path, opened from the root down, never via a link.

    The walk ends at the first missing component; a link or a file raises. The
    caller closes the descriptors.
    """
    root, *parts = Path(directory).parts
    opened = [os.open(root, _STEP)]
    try:
        for part in parts:
            try:
                opened.append(os.open(part, _STEP, dir_fd=opened[-1]))
            except FileNotFoundError:
                break
    except BaseException:
        for descriptor in opened:
            os.close(descriptor)
        raise
    return opened


def _target_id(directory: str, name: str, anchor: int, missing: int) -> str:
    """Digest of a store's real path and of the deepest directory on it that exists.

    anchor is that directory and missing counts the directories still missing
    below it: none once the store's own directory exists.
    """
    info = os.fstat(anchor)
    target = os.fsencode(os.path.join(directory, name))
    target += b"\0%d:%d:%d" % (missing, info.st_dev, info.st_ino)
    return hashlib.sha256(target).hexdigest()


def load_saved(
    path: Path | str, *, dir_fd: int | None = None
) -> tuple[tuple[SieveFilter, ...], str | None]:
    """The store's rules and raw SHA-256; a missing store holds none, anything invalid refuses.

    With dir_fd, path is the store's file name in that open directory.
    """
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dir_fd
        )
    except FileNotFoundError:
        return (), None
    except OSError:
        raise SavedRulesError(_INVALID) from None
    try:
        _private(descriptor, "Saved rules store")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_STORE_BYTES + 1)
    except OSError:
        raise SavedRulesError(_INVALID) from None
    finally:
        os.close(descriptor)
    return _parse(raw), hashlib.sha256(raw).hexdigest()


def _parse(raw: bytes) -> tuple[SieveFilter, ...]:
    try:
        if len(raw) > MAX_STORE_BYTES:
            raise ValueError
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "rules"}
            or type(data["version"]) is not int
            or data["version"] != VERSION
            or not isinstance(data["rules"], list)
            or len(data["rules"]) > MAX_SAVED_RULES
        ):
            raise ValueError
        rules = []
        for rule in data["rules"]:
            if not isinstance(rule, dict) or set(rule) != _RULE_KEYS:
                raise ValueError
            spec = filter_spec(**rule)
            # Only the canonical form add_rule writes, never a variant of it.
            if spec.params() != rule:
                raise ValueError
            rules.append(spec)
        if len({name_key(rule.name) for rule in rules}) != len(rules):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise SavedRulesError(_INVALID) from None
    return tuple(rules)


def _render(rules: tuple[SieveFilter, ...]) -> bytes:
    data = {"version": VERSION, "rules": [rule.params() for rule in rules]}
    return (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("ascii")


class LocalRulesBackend:
    """add_rule's default apply backend: append one reviewed rule to the local store.

    Implements the rules backend interface. Rules are read back from the
    reviewed script itself (sieve.parse_filter), so only exactly that rule can be
    stored. Each write replaces the store atomically with every existing rule,
    unchanged and in order, plus the new one, and is verified by reading it back.
    Tokens bind the store's real path and the deepest directory on it that
    exists (store_id), never a spelling of its path; bind() then holds that
    directory open, and a save uses no other or creates its own below it.
    """

    name = BACKEND

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        # While bind() holds a save: the store's directory, or the deepest one on
        # its path that existed then, open, and the directories missing below it.
        self._bound: tuple[int, tuple[str, ...]] | None = None

    @property
    def store_id(self) -> str:
        """The store target add_rule tokens bind (see _target_id), resolved now."""
        directory, name = _resolve(self.path)
        try:
            opened = _existing(directory)
        except OSError:
            raise SavedRulesError(_INVALID) from None
        try:
            missing = len(Path(directory).parts) - len(opened)
            return _target_id(directory, name, opened[-1], missing)
        finally:
            for descriptor in opened:
                os.close(descriptor)

    @contextmanager
    def bind(self, store_id: Any) -> Iterator[None]:
        """Hold a token's store for one save, refusing first unless it is still that store.

        Its path must resolve to the same real path, and the directory on it that
        the token bound, the store's own or else the deepest that existed at
        preview, must still be the same directory. Every read and the write until
        the save ends then use the store's directory as far as it exists now, held
        open, and create the rest below it without following a link, even if a
        link changes or a directory on the path is replaced meanwhile.
        """
        directory, name = _resolve(self.path)
        parts = Path(directory).parts
        try:
            opened = _existing(directory)
        except OSError:
            raise MailValidationError(_CHANGED) from None
        try:
            # A token previewed while the store's directory was missing names the
            # deepest directory that existed then, even once saving made the rest.
            current = {
                _target_id(directory, name, descriptor, len(parts) - depth)
                for depth, descriptor in enumerate(opened, start=1)
            }
            if not isinstance(store_id, str) or store_id not in current:
                raise MailValidationError(_CHANGED)
            # Each directory was opened inside the one above it, so the deepest is
            # the bound one or lies below it: the save holds that one.
            self._bound = (opened[-1], parts[len(opened) :])
            yield
        finally:
            self._bound = None
            for descriptor in opened:
                os.close(descriptor)

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int | None]:
        """The store's open directory: below the bound one, else resolved now; None if missing."""
        try:
            if self._bound is not None:
                descriptor = _descend(*self._bound, create=create)
            else:
                descriptor = _open_directory(_resolve(self.path)[0], create=create)
        except FileNotFoundError:
            if create:
                raise SavedRulesError(_INVALID) from None
            descriptor = None
        except OSError:
            raise SavedRulesError(_INVALID) from None
        try:
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _rules(self) -> tuple[SieveFilter, ...]:
        with self._directory() as directory:
            return () if directory is None else load_saved(self.path.name, dir_fd=directory)[0]

    def status(self) -> dict[str, Any]:
        try:
            rules = self._rules()
        except SavedRulesError:
            return {"available": False, "reason": "the saved rules store is invalid"}
        if len(rules) >= MAX_SAVED_RULES:
            return {
                "available": False,
                "reason": f"the saved rules store already holds {MAX_SAVED_RULES} rules",
            }
        return {"available": True, "reason": None}

    def filter_names(self, *, timeout: float) -> set[str]:
        return {rule.name for rule in self._rules()}

    def create_filter(self, name: str, script: str, *, timeout: float) -> str | None:
        spec = parse_filter(name, script)
        with self._directory(create=True) as directory, self._lock(directory):
            rules, _ = load_saved(self.path.name, dir_fd=directory)
            if name_key(spec.name) in {name_key(rule.name) for rule in rules}:
                raise MailValidationError(
                    "A saved rule with this name already exists; rules are never replaced"
                )
            if len(rules) >= MAX_SAVED_RULES:
                raise MailValidationError(
                    f"The saved rules store already holds {MAX_SAVED_RULES} rules"
                )
            expected = (*rules, spec)
            payload = _render(expected)
            if len(payload) > MAX_STORE_BYTES:
                raise MailValidationError("The saved rules store would exceed its size limit")
            # Never write a store that would not read back as every rule plus this one.
            if _parse(payload) != expected:
                raise SavedRulesError("Saved rules store would not keep its rules; nothing written")
            self._replace(directory, payload)
            if load_saved(self.path.name, dir_fd=directory)[0] != expected:
                raise SavedRulesError("Saved rules store did not keep exactly the new rule")
        return None

    @contextmanager
    def _lock(self, directory: int) -> Iterator[None]:
        """Serialize writers across processes; readers rely on the atomic rename."""
        descriptor = os.open(
            self.path.name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            # Only ever make this store's own lock private: check the file first.
            _owned(descriptor, "Saved rules lock")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MailValidationError("Saved rules store is busy; retry") from None
            yield
        finally:
            os.close(descriptor)

    def _replace(self, directory: int, payload: bytes) -> None:
        """Write a private temporary file, then rename it over the store in one step."""
        name = f".saved-rules-{secrets.token_hex(8)}"
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
