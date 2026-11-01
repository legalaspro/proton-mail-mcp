"""The Windows access-rule checks, with a stand-in GetSecurityInfo that hands back real ACL bytes.

The owners and rules here are the ones Windows reports, built byte for byte as it lays them out,
and the code under test walks those bytes through ctypes pointers as it does on Windows. This shows
what the checks decide about given rules, not what Windows reports for a given file: test_localfs.py
asks the real API where Windows, or Wine, is really there.
"""

import os
import struct
import sys
from types import SimpleNamespace

import pytest
from test_localfs_windows import (
    ADMINISTRATORS,
    ALLOW,
    AUTHENTICATED,
    CAPABILITY,
    CONTAINER_INHERIT,
    CREATOR_OWNER,
    DENY,
    ERROR_ACCESS_DENIED,
    ERROR_SHARING_VIOLATION,
    EVERYONE,
    FULL,
    INHERIT_ONLY,
    INHERITED,
    ME,
    MODIFY,
    OBJECT_INHERIT,
    OTHER_USER,
    OWNER_RIGHTS,
    READ,
    SYSTEM,
    USERS,
    ace,
    acl,
    kernel32,  # noqa: F401
    owner_only,
    profile_folder,
    python_0700,
    shared,
    sid_bytes,
)

from proton_mail_mcp import attachments, config, identity_index, localfs, service_entry
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.installer import cli as installer_cli
from proton_mail_mcp.installer.detect import simulated
from proton_mail_mcp.installer.plan import PlanError
from proton_mail_mcp.installer.probe import simulated_probe
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.rules_store import LocalRulesBackend, SavedRulesError, load_saved
from proton_mail_mcp.rules_timer import _private_file
from proton_mail_mcp.search_index import SearchIndex, _Unavailable
from proton_mail_mcp.service_entry import EnvFileError, read_env_file

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the stand-ins are POSIX calls; test_localfs.py runs here"
)
BASE = {"PROTON_MCP_TOKEN": "t" * 43, "PROTON_BRIDGE_USER": "reader@example.com"}
USER = "you@example.com"


@pytest.fixture
def security(kernel32):  # noqa: F811
    return kernel32.security


@pytest.fixture
def home(kernel32, monkeypatch, tmp_path):  # noqa: F811
    """A profile as Windows lays it out, with its own access rules from the fake's default."""
    root = tmp_path / "Users" / "me"
    (root / "AppData" / "Local").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda _path: str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "AppData" / "Local"))
    kernel32.security.set(root, profile_folder())
    return root


def opened(path, flags=os.O_RDONLY):
    return localfs.open(path, flags, 0o600)


def verdict(tmp_path, security, rules):
    path = tmp_path / "file"
    path.touch()
    security.set(path, rules)
    descriptor = opened(path)
    try:
        return localfs.acl_refusal(descriptor)
    finally:
        os.close(descriptor)


# What the rules say -----------------------------------------------------------


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param(owner_only(), id="owner-only"),
        pytest.param(python_0700(), id="python-mkdir-0700"),
        pytest.param(profile_folder(), id="inherited-profile"),
        pytest.param((ME, acl()), id="empty-dacl-owner-only-by-ownership"),
        pytest.param((ME, acl(ace(ME), ace(EVERYONE, 0))), id="an-empty-mask-grants-nothing"),
        pytest.param((ME, acl(ace(EVERYONE, READ, kind=DENY), ace(ME))), id="deny-everyone"),
        pytest.param(
            (ME, acl(ace(ME), ace(CREATOR_OWNER, FULL, INHERIT_ONLY | OBJECT_INHERIT))),
            id="creator-owner-for-new-files",
        ),
        pytest.param((SYSTEM, acl(ace(SYSTEM), ace(ME))), id="owned-by-system"),
        pytest.param((ADMINISTRATORS, acl(ace(ADMINISTRATORS), ace(ME))), id="owned-by-admins"),
    ],
)
def test_only_this_user_system_and_administrators_is_private(tmp_path, security, rules):
    assert verdict(tmp_path, security, rules) is None


@pytest.mark.parametrize(
    ("rules", "named"),
    [
        pytest.param(shared(EVERYONE, READ), "Everyone (S-1-1-0)", id="everyone-read"),
        pytest.param(shared(USERS, READ), "Users (S-1-5-32-545)", id="users-read"),
        pytest.param(shared(AUTHENTICATED, MODIFY), "Authenticated Users", id="authenticated"),
        pytest.param(shared(OTHER_USER, READ), OTHER_USER, id="another-user-read"),
        pytest.param(shared(OTHER_USER, 0x116), OTHER_USER, id="another-user-write-only"),
        pytest.param(shared(EVERYONE, READ, INHERITED), "Everyone", id="inherited-everyone"),
        pytest.param(
            shared(USERS, READ, INHERIT_ONLY | CONTAINER_INHERIT | OBJECT_INHERIT),
            "Users",
            id="inherit-only-for-the-next-file",
        ),
        pytest.param(
            (ME, acl(ace(EVERYONE, READ, kind=DENY), ace(EVERYONE, READ))),
            "Everyone",
            id="a-deny-rule-does-not-excuse-an-allow-rule",
        ),
        pytest.param((OTHER_USER, acl(ace(ME))), "owned by another account", id="other-owner"),
        pytest.param((ME, None), "no access rules", id="null-dacl"),
        pytest.param((None, acl(ace(ME))), "no owner", id="no-owner"),
    ],
)
def test_anyone_else_refuses_and_is_named(tmp_path, security, rules, named):
    refusal = verdict(tmp_path, security, rules)
    assert refusal.startswith("must be private on Windows, but it ") and named in refusal
    assert "docs/windows.md" in refusal


def rule_of_kind(kind):
    return struct.pack("<BBH", kind, 0, 8 + 12) + struct.pack("<I", READ) + sid_bytes(SYSTEM)


@pytest.mark.parametrize(
    ("dacl", "named"),
    [
        pytest.param(acl(rule_of_kind(5)), "kind that cannot be checked", id="object-allow"),
        pytest.param(acl(rule_of_kind(9)), "kind that cannot be checked", id="callback-allow"),
        pytest.param(
            struct.pack("<BBHHH", 2, 0, 8, 1, 0) + rule_of_kind(0), "cannot be read", id="past-end"
        ),
        pytest.param(
            struct.pack("<BBHHH", 2, 0, 12, 1, 0) + struct.pack("<BBH", 0, 0, 2) + b"\0" * 8,
            "cannot be read",
            id="a-rule-too-short-to-move-past",
        ),
        pytest.param(
            acl(struct.pack("<BBH", ALLOW, 0, 40) + struct.pack("<I", READ) + sid_bytes(SYSTEM)),
            "cannot be read",
            id="a-rule-longer-than-its-list",
        ),
        pytest.param(
            acl(struct.pack("<BBHI", ALLOW, 0, 8, READ)),
            "cannot be read",
            id="an-allow-rule-with-no-sid",
        ),
        pytest.param(
            acl(struct.pack("<BBHI", ALLOW, 0, 16, READ) + b"\x01\x0f" + b"\0" * 6),
            "cannot be read",
            id="sid-longer-than-its-rule",
        ),
    ],
)
def test_rules_that_cannot_be_read_with_certainty_refuse(tmp_path, security, dacl, named):
    assert named in verdict(tmp_path, security, (ME, dacl))


def test_a_failing_security_call_refuses(tmp_path, security):
    security.error = ERROR_ACCESS_DENIED
    assert "cannot be read" in verdict(tmp_path, security, owner_only())
    assert not security.freed


def test_a_token_that_cannot_be_read_refuses(tmp_path, security):
    security.token_error = ERROR_ACCESS_DENIED
    assert "cannot be read" in verdict(tmp_path, security, owner_only())


def test_a_longer_sid_is_compared_whole(tmp_path, security):
    rules = (f"{ME}-1", acl(ace(ME)))
    assert "owned by another account" in verdict(tmp_path, security, rules)
    assert "another account" in verdict(tmp_path, security, (ME, acl(ace(f"{ME}-1"))))


def test_the_call_asks_for_owner_and_rules_by_handle_and_frees_the_memory(tmp_path, kernel32):  # noqa: F811
    security = kernel32.security
    path = tmp_path / "file"
    path.touch()
    descriptor = opened(path)
    assert localfs.acl_refusal(descriptor) is None
    # The handle, SE_FILE_OBJECT, OWNER | DACL; no group and no audit rules wanted.
    assert security.calls == [(descriptor, 1, 1 | 4, None, None)]
    assert len(security.freed) == 1
    os.close(descriptor)


def test_this_user_comes_from_the_process_token_once_and_its_handle_closes(
    tmp_path,
    kernel32,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("USERNAME", "someone-else")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    closed = len(kernel32.closed_handles)
    assert localfs._my_sid() == ME
    assert localfs._my_sid() == ME
    assert len(kernel32.closed_handles) == closed + 1


def test_a_file_a_directory_and_a_write_handle_are_each_checked(tmp_path, security):
    file = tmp_path / "file"
    file.touch()
    folder = tmp_path / "folder"
    folder.mkdir()
    security.set(file, shared())
    security.set(folder, shared())
    for descriptor in (
        opened(file, os.O_WRONLY),
        opened(file, os.O_RDWR),
        localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY),
    ):
        assert localfs.acl_refusal(descriptor)
        os.close(descriptor)


def test_owner_and_mode_checks_cannot_stand_for_the_rules(tmp_path, security):
    # What Windows stat says of a file open to everyone is what it says of a private one.
    path = tmp_path / "file"
    path.touch()
    security.set(path, shared())
    descriptor = opened(path)
    info = os.fstat(descriptor)
    assert localfs.owned(info) and localfs.mode_private(info) and not localfs.world_writable(info)
    assert localfs.acl_refusal(descriptor)
    os.close(descriptor)


# Names and parents ------------------------------------------------------------


def test_a_name_is_checked_in_its_open_directory_and_not_followed(tmp_path, security):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "file").touch()
    (tmp_path / "shared").touch()
    (folder / "link").symlink_to(tmp_path / "shared")
    security.set(folder / "file", shared())
    directory = localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY)
    assert localfs.name_refusal("file", dir_fd=directory)
    with pytest.raises(OSError):
        localfs.name_refusal("link", dir_fd=directory)
    with pytest.raises(FileNotFoundError):
        localfs.name_refusal("missing", dir_fd=directory)
    os.close(directory)


def test_a_folder_is_checked_for_what_a_new_file_would_take_on(tmp_path, security):
    assert localfs.parent_refusal(tmp_path / "file") is None
    assert localfs.parent_refusal(tmp_path / "missing" / "file") is None
    security.set(tmp_path, shared())
    assert "its folder gives access to Everyone" in localfs.parent_refusal(tmp_path / "file")


def test_a_folder_that_cannot_be_opened_refuses(tmp_path, kernel32):  # noqa: F811
    kernel32.open_error = ERROR_ACCESS_DENIED
    assert "its folder has access rules that cannot be read" in localfs.parent_refusal(
        tmp_path / "file"
    )


def test_a_folder_held_by_another_program_says_so(tmp_path, kernel32):  # noqa: F811
    kernel32.open_error = ERROR_SHARING_VIOLATION
    refusal = localfs.parent_refusal(tmp_path / "file")
    assert "its folder is held open by another program" in refusal
    assert "cannot be read" not in refusal


# Paths ------------------------------------------------------------------------


def test_a_fresh_profile_passes_as_far_as_anything_exists(home):
    local = home / "AppData" / "Local"
    assert localfs.path_refusal(local / "proton-mail-mcp" / "state" / "actions.jsonl") is None
    assert localfs.path_refusal(local) is None
    assert localfs.path_refusal(home) is None


def test_a_shared_folder_inside_a_private_profile_refuses(home, security):
    folder = home / "AppData" / "Local" / "shared"
    folder.mkdir()
    security.set(folder, shared(USERS, MODIFY))
    for path in (folder, folder / "actions.jsonl", folder / "new" / "actions.jsonl"):
        refusal = localfs.path_refusal(path)
        assert f"{folder} gives access to Users" in refusal


def test_a_file_moved_in_keeps_its_rules_and_refuses(home, security, tmp_path):
    elsewhere = tmp_path / "actions.jsonl"
    elsewhere.touch()
    security.set(elsewhere, shared(OTHER_USER, READ))
    moved = home / "actions.jsonl"
    os.replace(elsewhere, moved)
    assert f"{moved} gives access to another account" in localfs.path_refusal(moved)


def test_a_file_made_in_a_shared_folder_inherits_and_refuses(home, security):
    folder = home / "shared"
    folder.mkdir()
    security.set(folder, shared(EVERYONE, READ, OBJECT_INHERIT))
    descriptor = opened(folder / "new.jsonl", os.O_RDWR | os.O_CREAT)
    assert "Everyone" in localfs.acl_refusal(descriptor)
    os.close(descriptor)


def test_local_app_data_pointing_at_a_shared_place_refuses(home, security, tmp_path, monkeypatch):
    place = tmp_path / "D-drive" / "Public"
    place.mkdir(parents=True)
    security.set(place, shared(AUTHENTICATED, MODIFY))
    monkeypatch.setenv("LOCALAPPDATA", str(place))
    state = localfs.state_home()
    assert str(state).startswith(str(place))
    assert f"{place} gives access to Authenticated Users" in localfs.path_refusal(state)


def test_local_app_data_elsewhere_counts_when_its_rules_are_private(home, tmp_path, monkeypatch):
    place = tmp_path / "D-drive" / "mine"
    place.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(place))
    assert localfs.path_refusal(place / "proton-mail-mcp") is None


def test_a_profile_root_the_environment_names_is_checked_too(home, security, tmp_path, monkeypatch):
    # USERPROFILE at C:\ would otherwise bless the whole drive.
    security.set(tmp_path, shared(AUTHENTICATED, MODIFY))
    monkeypatch.setattr(os.path, "expanduser", lambda _path: str(tmp_path))
    assert f"{tmp_path} gives access" in localfs.path_refusal(tmp_path / "anywhere" / "file")


def test_the_walk_ends_at_the_profile_folder(home, security):
    # C:\Users and C:\ are open to everyone on every Windows, and say nothing about a profile.
    security.set(home.parent, shared(EVERYONE, READ))
    assert localfs.path_refusal(home / "AppData" / "Local" / "file") is None


def test_the_outermost_profile_folder_is_where_the_walk_ends(home, security):
    security.set(home, shared(EVERYONE, READ))
    security.set(home / "AppData", profile_folder())
    security.set(home / "AppData" / "Local", profile_folder())
    assert f"{home} gives access" in localfs.path_refusal(home / "AppData" / "Local" / "file")


def test_outside_the_profile_refuses_whatever_its_rules(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert localfs.path_refusal(outside) == "must be inside your user profile on Windows"


def test_a_link_out_of_the_profile_refuses(home, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (home / "link").symlink_to(outside)
    assert "inside your user profile" in localfs.path_refusal(home / "link" / "file")


def test_a_link_inside_the_profile_is_judged_by_where_it_leads(home, security):
    target = home / "shared"
    target.mkdir()
    security.set(target, shared())
    (home / "link").symlink_to(target)
    assert f"{target} gives access" in localfs.path_refusal(home / "link" / "file")


def test_a_step_that_cannot_be_opened_refuses(home, kernel32):  # noqa: F811
    kernel32.open_error = ERROR_ACCESS_DENIED
    assert "cannot be read" in localfs.path_refusal(home / "file")


def test_a_step_held_by_another_program_says_so(home, kernel32):  # noqa: F811
    kernel32.open_error = ERROR_SHARING_VIOLATION
    refusal = localfs.path_refusal(home / "file")
    assert "held open by another program" in refusal and "cannot be read" not in refusal


def test_a_walk_that_never_meets_its_root_stops_at_the_drive(home, monkeypatch):
    # A link moved after profile_root looked. The walk's own dirname ends where it starts.
    monkeypatch.setattr(localfs, "profile_root", lambda _path: str(home / "elsewhere"))
    real, opened = localfs._open_either, []

    def counted(path):
        opened.append(path)
        assert len(opened) < 100, "the walk did not stop"
        return real(path)

    monkeypatch.setattr(localfs, "_open_either", counted)
    assert localfs.path_refusal(home) == "must be inside your user profile on Windows"


def test_a_real_profile_folder_with_an_app_capability_passes(home, security):
    owner, dacl = shared(CAPABILITY, READ)
    security.set(home, (owner, dacl))
    security.set(home / "AppData", profile_folder())
    security.set(home / "AppData" / "Local", profile_folder())
    assert localfs.path_refusal(home) is None
    assert localfs.path_refusal(home / "AppData" / "Local" / "file") is None


def test_an_app_capability_refuses_anywhere_below_the_profile_folder(home, security):
    folder = home / "AppData" / "Local"
    file = folder / "actions.jsonl"
    file.touch()
    security.set(file, shared(CAPABILITY, READ))
    assert f"{file} gives access to another account" in localfs.path_refusal(file)
    assert "another account" in verdict(folder, security, shared(CAPABILITY, READ))
    security.set(file, owner_only())
    security.set(folder, shared(CAPABILITY, READ))
    assert f"{folder} gives access" in localfs.path_refusal(file)


def test_the_profile_folder_still_refuses_accounts_and_other_app_rules(home, security):
    # A package SID is not a capability: ALL APPLICATION PACKAGES, for one.
    for who in (EVERYONE, USERS, OTHER_USER, "S-1-15-2-1"):
        security.set(home, shared(who, READ))
        security.set(home / "AppData", profile_folder())
        security.set(home / "AppData" / "Local", profile_folder())
        assert f"{home} gives access" in localfs.path_refusal(home / "AppData")


def test_a_file_at_the_end_of_a_path_is_checked_as_a_file(home, security):
    file = home / "settings.env"
    file.touch()
    security.set(file, shared())
    assert f"{file} gives access" in localfs.path_refusal(file)


# Every place that trusts a file or a folder -----------------------------------


@pytest.fixture
def log_path(home):
    folder = home / "AppData" / "Local" / "state"
    folder.mkdir()
    return folder / "actions.jsonl"


def test_the_action_log_refuses_a_shared_file_before_reading_or_locking(
    log_path,
    kernel32,  # noqa: F811
):
    log_path.write_bytes(b"")
    kernel32.security.set(log_path, shared(OTHER_USER, READ))
    for readonly in (False, True):
        with pytest.raises(MailError, match="action log must be private on Windows"):
            with ActionLog(log_path).transaction(readonly=readonly):
                pytest.fail("trusted")
    assert not kernel32.lock_flags


def test_the_action_log_refuses_a_shared_folder_and_makes_nothing_in_it(log_path, security):
    security.set(log_path.parent, shared(USERS, MODIFY, OBJECT_INHERIT))
    with pytest.raises(MailError, match="its folder gives access to Users"):
        with ActionLog(log_path).transaction():
            pytest.fail("trusted")
    assert not log_path.exists()


def test_the_action_log_works_with_private_rules(log_path):
    with ActionLog(log_path).transaction() as transaction:
        assert transaction is not None
    assert log_path.exists()


def test_the_action_log_reads_with_private_rules(log_path):
    log_path.write_bytes(b"")
    with ActionLog(log_path).transaction(readonly=True) as transaction:
        assert transaction is not None


def test_the_rules_state_refuses_a_shared_file_and_folder(log_path, security):
    log_path.write_bytes(b"")
    security.set(log_path, shared())
    with pytest.raises(MailError, match="Rules state must be private on Windows"):
        _private_file(log_path, os.O_RDWR)
    security.set(log_path, owner_only())
    assert os.close(_private_file(log_path, os.O_RDWR)) is None
    security.set(log_path.parent, shared())
    with pytest.raises(MailError, match="its folder gives access"):
        _private_file(log_path, os.O_RDWR)


@pytest.mark.parametrize(
    "name", ["inventory.lock", "inventory.json"], ids=["its-lock", "its-inventory"]
)
def test_the_identity_index_refuses_a_shared_file(home, security, name):
    folder = home / "identity-index"
    folder.mkdir()
    (folder / name).touch()
    security.set(folder / name, shared())
    directory = localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY)
    descriptor = localfs.open(name, os.O_RDONLY, dir_fd=directory)
    with pytest.raises(identity_index._Uncertified):
        identity_index._private(descriptor)
    os.close(descriptor)
    os.close(directory)
    if name.endswith(".json"):
        directory = localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY)
        with pytest.raises(identity_index._Uncertified):
            identity_index._load(directory, "inventory")
        os.close(directory)


def test_the_identity_index_refuses_a_shared_folder_before_making_a_lock(home, security):
    folder = home / "identity-index"
    folder.mkdir()
    security.set(folder, shared(USERS, READ))
    with pytest.raises(identity_index._Uncertified):
        with identity_index._storage(folder, "key"):
            pytest.fail("trusted")
    assert not list(folder.iterdir())


def test_saved_rules_refuse_a_shared_store(home, security):
    store = home / "saved-rules.json"
    store.write_bytes(b'{"version":1,"rules":[]}')
    security.set(store, shared(OTHER_USER, MODIFY))
    with pytest.raises(SavedRulesError, match="Saved rules store must be private on Windows"):
        load_saved(store)


def test_saved_rules_refuse_a_shared_folder_and_a_shared_lock(home, security):
    folder = home / "rules"
    folder.mkdir()
    store = folder / "saved-rules.json"
    security.set(folder, shared())
    backend = LocalRulesBackend(store)
    with pytest.raises(SavedRulesError, match="Saved rules folder must be private"):
        with backend._directory():
            pytest.fail("trusted")
    assert backend.status()["available"] is False
    security.set(folder, owner_only())
    (folder / "saved-rules.json.lock").touch()
    security.set(folder / "saved-rules.json.lock", shared())
    directory = localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY)
    with pytest.raises(SavedRulesError, match="Saved rules lock must be private"):
        with backend._lock(directory):
            pytest.fail("trusted")
    os.close(directory)


def index_at(folder):
    index = object.__new__(SearchIndex)
    index.path = folder / "account.sqlite3"
    index._candidate_lock = None
    index.enabled = True
    index.reason = None
    return index


@pytest.fixture
def index_folder(home):
    folder = home / "search-index"
    folder.mkdir()
    return folder


def test_the_search_index_refuses_a_shared_folder(index_folder, security):
    security.set(index_folder, shared(USERS, READ))
    index = index_at(index_folder)
    with pytest.raises(_Unavailable, match="unsafe_permissions"):
        with index._locked(create=True):
            pytest.fail("trusted")
    assert not list(index_folder.iterdir())
    assert index.seed_uid("INBOX", "1", "<a@b>") is None
    with pytest.raises(_Unavailable, match="unsafe_permissions"):
        index.acquire_reindex_guard()


def test_the_search_index_refuses_a_shared_lock(index_folder, security):
    index = index_at(index_folder)
    (index_folder / "account.sqlite3.lock").touch()
    (index_folder / "account.sqlite3.reindex.lock").touch()
    security.set(index_folder / "account.sqlite3.lock", shared())
    security.set(index_folder / "account.sqlite3.reindex.lock", shared())
    with pytest.raises(_Unavailable, match="unsafe_permissions"):
        with index._locked():
            pytest.fail("trusted")
    with pytest.raises(_Unavailable, match="unsafe_permissions"):
        index.acquire_reindex_guard()


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_the_search_index_refuses_each_shared_database_file(index_folder, security, suffix):
    index = index_at(index_folder)
    path = index_folder / f"account.sqlite3{suffix}"
    path.touch()
    directory = localfs.open(index_folder, os.O_RDONLY | localfs.O_DIRECTORY)
    assert index._check_files(directory) == {path.name}
    security.set(path, shared(OTHER_USER, READ))
    with pytest.raises(_Unavailable, match="unsafe_permissions"):
        index._check_files(directory)
    os.close(directory)


@pytest.fixture
def attachment_settings(home):
    folder = home / "Documents" / "files"
    folder.mkdir(parents=True)
    return SimpleNamespace(attachments_dir=str(folder), attachments_dir_default=False)


def test_a_shared_attachments_folder_refuses_saving_and_reading(attachment_settings, security):
    folder = attachment_settings.attachments_dir
    security.set(folder, shared(USERS, READ, OBJECT_INHERIT))
    with pytest.raises(MailValidationError, match="must be private on Windows.*Users"):
        attachments._write_new(attachment_settings, "report.pdf", b"mail")
    assert not os.listdir(folder)
    with pytest.raises(MailValidationError, match="must be private on Windows"):
        attachments._open_root(attachment_settings)


def test_a_private_attachments_folder_saves(attachment_settings):
    assert attachments._write_new(attachment_settings, "report.pdf", b"mail") == "report.pdf"


@pytest.mark.parametrize(
    "name",
    [
        "PROTON_MCP_ACTION_LOG",
        "PROTON_MCP_SAVED_RULES",
        "PROTON_MCP_IDENTITY_INDEX_DIR",
        "PROTON_MCP_ATTACHMENTS_DIR",
    ],
)
def test_settings_refuse_a_shared_folder_inside_the_profile(home, security, name):
    folder = home / "shared"
    folder.mkdir()
    security.set(folder, shared(USERS, READ))
    base = {**BASE, "PROTON_MCP_ATTACHMENTS_DIR": str(home / "files")}
    with pytest.raises(ConfigError, match=f"{name} must be private on Windows.*Users"):
        Settings.from_env({**base, name: str(folder / "data")})
    assert Settings.from_env({**base, name: str(home / "data")})


def test_settings_refuse_a_shared_default_state_folder(home, security, tmp_path, monkeypatch):
    place = tmp_path / "Public"
    place.mkdir()
    security.set(place, shared(AUTHENTICATED, MODIFY))
    monkeypatch.setenv("LOCALAPPDATA", str(place))
    base = {**BASE, "PROTON_MCP_ATTACHMENTS_DIR": str(home / "files")}
    with pytest.raises(ConfigError, match="default state folder must be private on Windows"):
        Settings.from_env(base)
    # With every state path named, no file goes in the default folder.
    assert Settings.from_env(
        {
            **base,
            "PROTON_MCP_ACTION_LOG": str(home / "log"),
            "PROTON_MCP_IDENTITY_INDEX_DIR": str(home / "index"),
        }
    )


def test_settings_refuse_the_default_attachments_folder_when_shared(home, security, monkeypatch):
    folder = home / "AppData" / "Local"
    # The real default is a Windows path, which this host's file system cannot open.
    monkeypatch.setattr(config, "windows_attachments_dir", lambda _env: str(folder / "files"))
    security.set(folder, shared())
    with pytest.raises(ConfigError, match="ATTACHMENTS_DIR must be private on Windows"):
        Settings.from_env(BASE)


@pytest.fixture
def env_file(home):
    path = home / "settings.env"
    path.write_text(f"PROTON_BRIDGE_USER={USER}\n", encoding="utf-8")
    path.chmod(0o600)  # os.name stays posix here, so read_env_file's POSIX check runs as well.
    return path


def test_the_env_file_is_read_when_its_rules_are_private(env_file):
    assert read_env_file(env_file) == {"PROTON_BRIDGE_USER": USER}


@pytest.mark.parametrize(
    "who", [EVERYONE, USERS, OTHER_USER], ids=["everyone", "users", "another-user"]
)
def test_a_shared_env_file_refuses_without_echoing_its_values(env_file, security, who):
    security.set(env_file, shared(who, MODIFY))
    with pytest.raises(EnvFileError, match="Env file must be private on Windows") as refused:
        read_env_file(env_file)
    assert USER not in str(refused.value)


def test_an_env_file_in_a_shared_folder_refuses(home, security):
    folder = home / "config"
    folder.mkdir()
    path = folder / "settings.env"
    path.write_text(f"PROTON_BRIDGE_USER={USER}\n", encoding="utf-8")
    path.chmod(0o600)
    security.set(path, owner_only())
    security.set(folder, shared(USERS, READ))
    with pytest.raises(EnvFileError, match=f"{folder} gives access to Users"):
        read_env_file(path)


def test_the_env_file_handle_read_is_a_handle_checked(env_file, security, monkeypatch):
    # The file named at the first check is swapped for one that is open to others.
    monkeypatch.setattr(localfs, "path_refusal", lambda _path: None)
    security.set(env_file, shared())
    with pytest.raises(EnvFileError, match="Env file must be private on Windows"):
        read_env_file(env_file)


def test_the_service_log_refuses_a_shared_file_and_folder(home, security, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    folder = home / "logs"
    folder.mkdir()
    log = folder / "mcp.log"
    log.touch()
    security.set(log, shared())
    with pytest.raises(SystemExit) as stopped:
        service_entry._redirect_output(str(log))
    assert stopped.value.code == 2
    assert "log file must be private on Windows" in capsys.readouterr().err
    security.set(folder, shared(USERS, READ))
    with pytest.raises(SystemExit):
        service_entry._redirect_output(str(folder / "new.log"))
    assert "its folder gives access to Users" in capsys.readouterr().err
    assert not (folder / "new.log").exists()


def test_setup_refuses_a_shared_attachments_folder_on_the_real_host(home, security, monkeypatch):
    folder = home / "files"
    folder.mkdir()
    security.set(folder, shared(USERS, READ))
    check = localfs.path_refusal
    # The plan wants a Windows path, and this host's stand-in file system a POSIX one.
    monkeypatch.setattr(localfs, "path_refusal", lambda _path: check(folder))
    platform = simulated("windows")
    probe = simulated_probe(platform)

    def choose(simulate):
        arguments = SimpleNamespace(attachments_dir="C:\\Users\\you\\files", simulate=simulate)
        return installer_cli._choose_attachments(arguments, platform, probe, {}, False)

    with pytest.raises(PlanError, match="must be private on Windows.*Nothing was changed"):
        choose(None)
    assert choose("windows")[0] == "C:\\Users\\you\\files"


def test_setup_checks_folders_only_for_a_real_windows_host(home, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(installer_cli, "_shared_folder", lambda paths: seen.append(paths) or "no")
    # A test or a container may stand in another system: its paths are not this host's.
    monkeypatch.setattr(installer_cli, "detect_host", lambda: simulated("debian"))
    monkeypatch.setattr(installer_cli, "_windows_refusal", lambda _args: None)
    monkeypatch.setattr(installer_cli, "_leave_current_folder", lambda: None)
    monkeypatch.setattr(installer_cli, "HostProbe", simulated_probe)
    installer_cli.main(["install", "--dry-run", "--bridge-user", USER, "--skip-services"])
    assert not seen
    monkeypatch.setattr(installer_cli, "detect_host", lambda: simulated("windows"))
    assert installer_cli.main(["install", "--dry-run", "--bridge-user", USER]) == 2
    assert seen and "no" in capsys.readouterr().err


def test_setup_refuses_a_shared_folder_before_it_writes_anything(home, security):
    private = str(home / "AppData" / "Local" / "proton-mail-mcp")
    paths = SimpleNamespace(
        config_dir=private, data_dir=private, state_dir=private + "\\state", log_dir=None
    )
    assert installer_cli._shared_folder(paths) is None
    folder = home / "shared"
    folder.mkdir()
    security.set(folder, shared())
    paths.log_dir = str(folder)
    refusal = installer_cli._shared_folder(paths)
    assert refusal.startswith(str(folder)) and refusal.endswith("Nothing was changed")


def test_a_stand_in_rule_really_is_the_documented_layout():
    # AceType, AceFlags, AceSize, Mask, then S-1-1-0: what any Windows ACL dump shows.
    assert ace(EVERYONE, READ, OBJECT_INHERIT) == bytes.fromhex(
        "0001140089001200010100000000000100000000"
    )
    assert localfs._sid_text(sid_bytes(ME)) == ME
    assert localfs._sid_text(sid_bytes(OWNER_RIGHTS)) == OWNER_RIGHTS


# POSIX is not touched ----------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: localfs.acl_refusal(-1),
        lambda: localfs.name_refusal("anything", dir_fd=-1),
        lambda: localfs.parent_refusal("/nowhere/file"),
        lambda: localfs.path_refusal("/nowhere/file"),
    ],
    ids=["acl_refusal", "name_refusal", "parent_refusal", "path_refusal"],
)
def test_on_posix_the_checks_call_nothing_at_all(monkeypatch, call):
    def forbidden(*_args, **_kwargs):
        pytest.fail("a POSIX path reached the file system or Windows")

    # Only for the call: pytest itself needs these the moment it returns.
    with monkeypatch.context() as patched:
        for name in ("open", "stat", "lstat", "fstat", "close", "getcwd"):
            patched.setattr(os, name, forbidden)
        patched.setattr(os.path, "realpath", forbidden)
        patched.setattr(os.path, "abspath", forbidden)
        patched.setattr(localfs, "_kernel32", forbidden)
        patched.setattr(localfs, "_advapi32", forbidden)
        result = call()
    assert result is None
