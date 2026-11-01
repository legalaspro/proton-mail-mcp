"""The saved rules store: strict reads, private atomic appends, nothing ever replaced."""

import json
import os

import pytest
from windows_skips import has_mode

from proton_mail_mcp import localfs, rules_store
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_store import (
    MAX_SAVED_RULES,
    LocalRulesBackend,
    SavedRulesError,
    load_saved,
    rule_sha256,
    store_path,
)
from proton_mail_mcp.sieve import filter_spec, render_script

pytestmark = pytest.mark.real_fsync

GITHUB = [{"field": "from", "contains": "notifications@github.com"}]
TOKEN = "store-test-token-" + "x" * 32


def spec(name="GitHub", folder="Folders/GitHub", conditions=GITHUB, **options):
    return filter_spec(name, folder, conditions, **options)


def save(backend, built):
    return backend.create_filter(built.name, render_script(built), timeout=5)


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    path.chmod(mode)


def stored(rules):
    return {"version": 1, "rules": [rule.params() for rule in rules]}


@pytest.fixture
def store(tmp_path):
    return tmp_path / "state" / "saved-rules.json"


def leftovers(store):
    return sorted(path.name for path in store.parent.iterdir() if path.name.startswith("."))


def test_missing_store_holds_no_rules_and_paths_follow_the_journal(tmp_path, monkeypatch):
    assert load_saved(tmp_path / "absent.json") == ((), None)
    journal = tmp_path / "logs" / "actions.jsonl"
    settings = Settings(token=TOKEN, bridge_user="u", action_log_path=str(journal))
    assert store_path(settings) == journal.with_name("saved-rules.json")
    configured = Settings(
        token=TOKEN, bridge_user="u", action_log_path=str(journal), saved_rules_path="/x/r.json"
    )
    assert str(store_path(configured)) == "/x/r.json"
    environment = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "u"}
    assert Settings.from_env(environment).saved_rules_path is None
    custom = Settings.from_env(environment | {"PROTON_MCP_SAVED_RULES": "/y/rules.json"})
    assert custom.saved_rules_path == "/y/rules.json"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    default = store_path(Settings(token=TOKEN, bridge_user="u"))
    assert default == tmp_path / "xdg" / "proton-mail-mcp" / "saved-rules.json"
    status = LocalRulesBackend(default).status()
    assert status == {"available": True, "reason": None} and not default.parent.exists()


def test_appends_exactly_each_reviewed_rule_privately_and_keeps_every_earlier_rule(store):
    backend = LocalRulesBackend(store)
    rules = [
        spec(),
        spec("Receipts", "Folders/Work/Receipts", [{"field": "subject", "is": "Receipt"}]),
        spec(
            "Any",
            "Folders/GitHub",
            [{"field": "to", "is": "me@example.test"}, {"field": "subject", "contains": "Rx"}],
            match="any",
            mark_read=True,
        ),
    ]
    snapshots = []
    for index, rule in enumerate(rules, start=1):
        assert save(backend, rule) is None
        loaded, digest = load_saved(store)
        assert loaded == tuple(rules[:index])
        snapshots.append(json.loads(store.read_text())["rules"])
        assert digest == rules_store.hashlib.sha256(store.read_bytes()).hexdigest()
    # Every write kept the earlier rules exactly, in order, and only appended one.
    assert all(later[: len(earlier)] == earlier for earlier, later in zip(snapshots, snapshots[1:]))
    assert json.loads(store.read_text()) == stored(rules)
    assert has_mode(store, 0o600)
    assert has_mode(store.parent, 0o700)
    assert has_mode(store.with_name(store.name + ".lock"), 0o600)
    assert leftovers(store) == []
    assert backend.filter_names(timeout=5) == {"GitHub", "Receipts", "Any"}


def test_rule_digest_names_the_canonical_rule():
    assert rule_sha256(spec()) == rule_sha256(filter_spec(**spec().params()))
    variants = [
        spec(name="Other"),
        spec(folder="Folders/Other"),
        spec(conditions=[{"field": "from", "is": "notifications@github.com"}]),
        spec(match="any"),
        spec(mark_read=True),
    ]
    assert len({rule_sha256(rule) for rule in [spec(), *variants]}) == 6


@pytest.mark.parametrize(
    "change",
    [
        lambda script: "# reviewed\n" + script,
        lambda script: script.replace('fileinto "GitHub";', "discard;"),
        lambda script: script.replace('fileinto "GitHub";', 'fileinto "GitHub";\n    keep;'),
        lambda script: script.replace(":contains", ":matches"),
        lambda script: script + 'redirect "x@example.test";\n',
    ],
    ids=["comment", "discard", "keep", "matches", "redirect"],
)
def test_only_the_rendered_script_of_a_valid_rule_can_be_saved(store, change):
    backend = LocalRulesBackend(store)
    with pytest.raises(MailValidationError, match="not a reviewed filter"):
        backend.create_filter("GitHub", change(render_script(spec())), timeout=5)
    assert not store.exists()


@pytest.mark.parametrize("name", ["GitHub", "github", "GITHUB", "ＧｉｔＨｕｂ"])
def test_names_in_use_are_refused_in_any_letter_case_or_width(store, name):
    backend = LocalRulesBackend(store)
    save(backend, spec())
    before = store.read_bytes()
    with pytest.raises(MailValidationError, match="already exists; rules are never replaced"):
        save(backend, spec(name=name, folder="Folders/Other"))
    assert store.read_bytes() == before


def test_the_store_holds_at_most_twenty_rules(store):
    backend = LocalRulesBackend(store)
    for index in range(MAX_SAVED_RULES):
        save(backend, spec(name=f"Rule {index}"))
    assert backend.status() == {
        "available": False,
        "reason": "the saved rules store already holds 20 rules",
    }
    before = store.read_bytes()
    with pytest.raises(MailValidationError, match="already holds 20 rules"):
        save(backend, spec(name="One more"))
    assert store.read_bytes() == before


VALID = stored([spec()])
INVALID = {
    "not json": b"{",
    "not utf-8": b'{"version": 1, "rules": ["\xff"]}',
    "list": [],
    "duplicate key": b'{"version": 1, "version": 1, "rules": []}',
    "missing rules": {"version": 1},
    "extra key": VALID | {"source": "INBOX"},
    "version 2": VALID | {"version": 2},
    "version true": VALID | {"version": True},
    "rules object": VALID | {"rules": {}},
    "rule extra key": {"version": 1, "rules": [spec().params() | {"source": "INBOX"}]},
    "rule missing key": {"version": 1, "rules": [{"name": "GitHub"}]},
    "system folder": {"version": 1, "rules": [spec().params() | {"folder": "Trash"}]},
    "label": {"version": 1, "rules": [spec().params() | {"folder": "Labels/GitHub"}]},
    "newline value": {
        "version": 1,
        "rules": [spec().params() | {"conditions": [{"field": "from", "is": "a\ndiscard;"}]}],
    },
    "variable": {
        "version": 1,
        "rules": [spec().params() | {"conditions": [{"field": "subject", "is": "${1}"}]}],
    },
    "string mark_read": {"version": 1, "rules": [spec().params() | {"mark_read": "yes"}]},
    "repeated name": stored([spec(), spec(name="GITHUB", folder="Folders/Other")]),
    "too many": stored([spec(name=f"Rule {index}") for index in range(MAX_SAVED_RULES + 1)]),
    "oversized": b" " * (rules_store.MAX_STORE_BYTES + 1),
}


@pytest.mark.parametrize("case", list(INVALID))
def test_an_invalid_store_fails_closed_everywhere(store, case):
    write(store, INVALID[case])
    before = store.read_bytes()
    for read in (lambda: load_saved(store), lambda: backend.filter_names(timeout=5)):
        backend = LocalRulesBackend(store)
        with pytest.raises(SavedRulesError, match="Cannot load valid saved rules") as error:
            read()
        assert isinstance(error.value, ConfigError) and isinstance(error.value, MailValidationError)
    assert backend.status() == {"available": False, "reason": "the saved rules store is invalid"}
    with pytest.raises(SavedRulesError):
        save(backend, spec(name="New"))
    assert store.read_bytes() == before


@pytest.mark.parametrize("kind", ["group-readable", "world-writable", "symlink", "hardlink", "dir"])
def test_the_store_must_be_a_private_regular_file(store, kind):
    write(store.with_name("real.json"), VALID)
    if kind in {"group-readable", "world-writable"}:
        write(store, VALID, 0o640 if kind == "group-readable" else 0o602)
    elif kind == "symlink":
        store.symlink_to(store.with_name("real.json"))
    elif kind == "hardlink":
        os.link(store.with_name("real.json"), store)
    else:
        store.mkdir()
    with pytest.raises(SavedRulesError):
        load_saved(store)
    backend = LocalRulesBackend(store)
    assert backend.status()["available"] is False
    with pytest.raises(SavedRulesError):
        save(backend, spec(name="New"))
    assert json.loads(store.with_name("real.json").read_text()) == VALID


def test_a_fifo_is_refused_without_blocking(store):
    store.parent.mkdir(parents=True)
    os.mkfifo(store, 0o600)
    with pytest.raises(SavedRulesError, match="private regular file"):
        load_saved(store)


class _FailingWrite:
    """A temporary file that takes a few bytes, then reports a full disk."""

    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def fileno(self):
        return self.stream.fileno()

    def write(self, data):
        self.stream.write(data[:7])
        raise OSError("synthetic disk full")


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_an_interrupted_write_keeps_the_previous_store_and_no_temporary_file(
    store, monkeypatch, failure
):
    backend = LocalRulesBackend(store)
    save(backend, spec())
    before = store.read_bytes()
    original = getattr(os, "fdopen" if failure == "write" else failure)

    def broken(*args, **kwargs):
        raise OSError("synthetic disk failure")

    if failure == "write":
        opened = os.fdopen

        def fdopen(descriptor, mode="r", *args, **kwargs):
            stream = opened(descriptor, mode, *args, **kwargs)
            return _FailingWrite(stream) if "w" in mode else stream

        monkeypatch.setattr(rules_store.os, "fdopen", fdopen)
    else:
        monkeypatch.setattr(rules_store.os, failure, broken)
    with pytest.raises(OSError, match="synthetic"):
        save(backend, spec(name="New"))
    # Restore only this patch: the suite's network guards stay in place.
    monkeypatch.setattr(rules_store.os, "fdopen" if failure == "write" else failure, original)
    assert store.read_bytes() == before and leftovers(store) == []
    assert load_saved(store)[0] == (spec(),)


def test_a_render_that_would_drop_a_rule_is_never_written(store, monkeypatch):
    backend = LocalRulesBackend(store)
    save(backend, spec())
    before = store.read_bytes()
    render = rules_store._render
    monkeypatch.setattr(rules_store, "_render", lambda rules: render(rules[-1:]))
    with pytest.raises(SavedRulesError, match="nothing written"):
        save(backend, spec(name="New"))
    assert store.read_bytes() == before and leftovers(store) == []


def test_a_held_lock_refuses_a_concurrent_writer(store):
    backend = LocalRulesBackend(store)
    save(backend, spec())
    before = store.read_bytes()
    descriptor = os.open(backend.lock_path, os.O_RDWR)
    try:
        localfs.lock(descriptor)
        with pytest.raises(MailValidationError, match="busy"):
            save(backend, spec(name="New"))
    finally:
        os.close(descriptor)
    assert store.read_bytes() == before
    save(backend, spec(name="New"))
    assert [rule.name for rule in load_saved(store)[0]] == ["GitHub", "New"]


@pytest.mark.parametrize("kind", ["hardlink", "symlink", "fifo"])
def test_a_lock_that_is_not_the_stores_own_is_refused_and_left_unchanged(store, kind):
    other = store.with_name("other")
    write(other, b"not a lock", 0o644)
    lock = store.with_name(store.name + ".lock")
    if kind == "hardlink":
        os.link(other, lock)
    elif kind == "symlink":
        lock.symlink_to(other)
    else:
        os.mkfifo(lock, 0o644)
    # A symlink's own mode is 0777 on Linux and follows the umask on macOS.
    before = lock.lstat().st_mode
    with pytest.raises((SavedRulesError, OSError)):
        save(LocalRulesBackend(store), spec())
    # Checked before any chmod: neither the linked file nor the FIFO became 0600.
    assert other.stat().st_mode & 0o777 == 0o644
    assert lock.lstat().st_mode == before
    assert not store.exists()


def test_a_lock_of_its_own_readable_by_others_is_made_private(store):
    lock = store.with_name(store.name + ".lock")
    write(lock, b"", 0o644)
    save(LocalRulesBackend(store), spec())
    assert has_mode(lock, 0o600) and load_saved(store)[0] == (spec(),)


def test_store_target_identity_is_a_digest_that_hides_the_path(store, tmp_path):
    first, second = LocalRulesBackend(store), LocalRulesBackend(tmp_path / "other.json")
    assert first.store_id == LocalRulesBackend(store).store_id != second.store_id
    assert len(first.store_id) == 64 and str(store) not in first.store_id


def linked(tmp_path):
    """first/linked -> second/nested, so first/linked/.. is second, not first."""
    first, nested = tmp_path / "first", tmp_path / "second" / "nested"
    first.mkdir()
    nested.mkdir(parents=True)
    (first / "linked").symlink_to(nested)
    return first, tmp_path / "second"


def refuses(backend, store_id):
    with pytest.raises(MailValidationError, match="store changed after preview; preview again"):
        with backend.bind(store_id):
            pytest.fail("bound a store that is not the token's")


def test_a_link_and_dotdot_spelling_of_another_directory_is_another_store(tmp_path):
    first, second = linked(tmp_path)
    plain = LocalRulesBackend(first / "rules.json")
    dotted = LocalRulesBackend(first / "linked" / ".." / "rules.json")
    # One file to abspath, which drops .. before the link resolves; two to the kernel.
    assert os.path.abspath(dotted.path) == str(plain.path)
    assert plain.store_id != dotted.store_id
    save(plain, spec())
    save(dotted, spec(name="Other"))
    assert load_saved(first / "rules.json")[0] == (spec(),)
    assert load_saved(second / "rules.json")[0] == (spec(name="Other"),)
    refuses(plain, dotted.store_id)
    refuses(dotted, plain.store_id)
    # Every spelling of one real store names the same store.
    (tmp_path / "alias").symlink_to(first)
    assert LocalRulesBackend(tmp_path / "alias" / "rules.json").store_id == plain.store_id
    assert LocalRulesBackend(second / "nested" / ".." / "rules.json").store_id == dotted.store_id


def test_a_bound_save_stays_in_its_directory_when_the_link_to_it_moves(tmp_path):
    first, second = linked(tmp_path)
    selected = tmp_path / "selected"
    selected.symlink_to(first)
    backend = LocalRulesBackend(selected / "rules.json")
    bound = backend.store_id
    with backend.bind(bound):
        selected.unlink()
        selected.symlink_to(second)
        assert backend.filter_names(timeout=5) == set()
        save(backend, spec())
        assert backend.filter_names(timeout=5) == {"GitHub"}
    assert load_saved(first / "rules.json")[0] == (spec(),)
    assert sorted(os.listdir(second)) == ["nested"]
    # Resolved again, the link names second's store, which the old binding is not.
    assert backend.store_id != bound and backend.filter_names(timeout=5) == set()
    refuses(backend, bound)


def test_a_bound_save_writes_only_into_the_directory_it_checked(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = LocalRulesBackend(state / "rules.json")
    with backend.bind(backend.store_id):
        # Swapped for another directory after the check: the save stays in the checked one.
        state.rename(tmp_path / "checked")
        state.mkdir()
        save(backend, spec())
    assert load_saved(tmp_path / "checked" / "rules.json")[0] == (spec(),)
    assert os.listdir(state) == []


def test_a_bound_save_creates_a_missing_directory_only_at_its_bound_real_path(tmp_path):
    first, second = linked(tmp_path)
    selected = tmp_path / "selected"
    selected.symlink_to(first)
    backend = LocalRulesBackend(selected / "new" / "rules.json")
    with backend.bind(backend.store_id):
        selected.unlink()
        selected.symlink_to(second)
        save(backend, spec())
    assert load_saved(first / "new" / "rules.json")[0] == (spec(),)
    assert sorted(os.listdir(second)) == ["nested"]


def test_a_store_directory_replaced_after_preview_is_another_store(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    backend = LocalRulesBackend(state / "rules.json")
    bound = backend.store_id
    state.rename(tmp_path / "old")
    state.mkdir()
    assert backend.store_id != bound
    refuses(backend, bound)
    assert os.listdir(state) == [] and os.listdir(tmp_path / "old") == []
    # Removed since preview: refused too, and nothing is created again.
    state.rmdir()
    refuses(backend, bound)
    assert not state.exists()


def test_a_directory_missing_at_preview_binds_the_deepest_existing_one_and_saving_creates_it(
    tmp_path,
):
    store = tmp_path / "new" / "state" / "rules.json"
    backend = LocalRulesBackend(store)
    bound = backend.store_id
    assert backend.status()["available"] is True and not (tmp_path / "new").exists()
    with backend.bind(bound):
        assert backend.filter_names(timeout=5) == set()
        save(backend, spec())
    assert load_saved(store)[0] == (spec(),)
    assert has_mode(store.parent, 0o700)
    # Bindings made now name the store's own directory; one made while it was
    # missing still names tmp_path, as a second rule previewed before the first save.
    assert backend.store_id != bound
    with backend.bind(bound):
        save(backend, spec(name="Second"))
    assert [rule.name for rule in load_saved(store)[0]] == ["GitHub", "Second"]
    # A link created where the directory was missing resolves elsewhere.
    later = LocalRulesBackend(tmp_path / "later" / "rules.json")
    bound = later.store_id
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "later").symlink_to(tmp_path / "elsewhere")
    refuses(later, bound)
    assert os.listdir(tmp_path / "elsewhere") == []


def missing_child(tmp_path):
    """parent exists and its child does not: the store is parent/missing-child/rules.json."""
    parent = tmp_path / "parent"
    parent.mkdir()
    return parent, LocalRulesBackend(parent / "missing-child" / "rules.json")


def replace_directory(directory, moved):
    """Move a directory away and make another one in its place."""
    directory.rename(moved)
    directory.mkdir()


def test_the_parent_of_a_missing_directory_replaced_after_preview_is_another_store(tmp_path):
    parent, backend = missing_child(tmp_path)
    old = tmp_path / "old-parent"
    bound = backend.store_id
    replace_directory(parent, old)
    assert backend.store_id != bound
    refuses(backend, bound)
    # Not even once the replacement holds the missing directory: nothing is written.
    (parent / "missing-child").mkdir()
    refuses(backend, bound)
    assert os.listdir(old) == [] and os.listdir(parent / "missing-child") == []
    # Moved back, the directory the token bound is its store again.
    (parent / "missing-child").rmdir()
    parent.rmdir()
    old.rename(parent)
    with backend.bind(bound):
        save(backend, spec())
    assert load_saved(backend.path)[0] == (spec(),)


def test_a_bound_save_creates_its_missing_directory_only_in_the_parent_it_checked(tmp_path):
    parent, backend = missing_child(tmp_path)
    with backend.bind(backend.store_id):
        # Replaced after the check by a parent whose store already uses the name.
        replace_directory(parent, tmp_path / "old-parent")
        save(LocalRulesBackend(backend.path), spec())
        before = backend.path.read_bytes()
        assert backend.filter_names(timeout=5) == set()
        save(backend, spec())
        assert backend.filter_names(timeout=5) == {"GitHub"}
    assert load_saved(tmp_path / "old-parent" / "missing-child" / "rules.json")[0] == (spec(),)
    assert backend.path.read_bytes() == before


def test_a_bound_save_keeps_to_a_directory_made_since_preview(tmp_path):
    parent, backend = missing_child(tmp_path)
    bound = backend.store_id
    with backend.bind(backend.store_id):
        save(backend, spec())
    with backend.bind(bound):
        # Made since preview, then swapped after the check: the save stays in it.
        replace_directory(parent / "missing-child", parent / "moved")
        save(backend, spec(name="Second"))
    moved = load_saved(parent / "moved" / "rules.json")[0]
    assert [rule.name for rule in moved] == ["GitHub", "Second"]
    assert os.listdir(parent / "missing-child") == []


def test_a_bound_directory_removed_after_the_check_refuses_the_save(tmp_path, monkeypatch):
    parent, backend = missing_child(tmp_path)
    monkeypatch.chdir(tmp_path)
    with backend.bind(backend.store_id):
        parent.rmdir()
        assert backend.filter_names(timeout=5) == set()
        with pytest.raises(SavedRulesError):
            save(backend, spec())
    # Nothing was written anywhere, not even relative to the working directory.
    assert os.listdir(tmp_path) == []


def test_a_link_appearing_below_the_bound_directory_is_never_followed(tmp_path):
    parent, backend = missing_child(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with backend.bind(backend.store_id):
        (parent / "missing-child").symlink_to(elsewhere)
        assert backend.status() == {
            "available": False,
            "reason": "the saved rules store is invalid",
        }
        with pytest.raises(SavedRulesError):
            save(backend, spec())
    assert os.listdir(elsewhere) == []


def test_a_link_in_the_resolved_directory_is_never_followed(tmp_path, monkeypatch):
    first, _ = linked(tmp_path)
    (tmp_path / "selected").symlink_to(first)
    for target, create in (("selected", False), ("selected/new", True), ("first/linked", False)):
        with pytest.raises(OSError):
            rules_store._open_directory(str(tmp_path / target), create=create)
        # Nor is a link the end of the existing path: it is neither entered nor missing.
        with pytest.raises(OSError):
            rules_store._existing(str(tmp_path / target))
    assert sorted(os.listdir(first)) == ["linked"]
    backend = LocalRulesBackend(tmp_path / "selected" / "rules.json")
    bound = backend.store_id
    # As if the link appeared just after its directory was resolved.
    monkeypatch.setattr(
        rules_store, "_resolve", lambda path: (os.path.abspath(path.parent), path.name)
    )
    with pytest.raises(SavedRulesError):
        backend.store_id
    assert backend.status() == {"available": False, "reason": "the saved rules store is invalid"}
    with pytest.raises(SavedRulesError):
        backend.filter_names(timeout=5)
    with pytest.raises(SavedRulesError):
        save(backend, spec())
    refuses(backend, bound)
    assert sorted(os.listdir(first)) == ["linked"]


def test_a_store_path_must_name_a_file(tmp_path):
    backend = LocalRulesBackend(tmp_path / "state" / "..")
    assert backend.status()["available"] is False
    with pytest.raises(SavedRulesError, match="must name a file"):
        backend.store_id
    with pytest.raises(SavedRulesError, match="must name a file"):
        save(backend, spec())
