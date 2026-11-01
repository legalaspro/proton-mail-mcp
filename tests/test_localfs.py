"""The local file seam, on the real filesystem of whatever platform runs the tests.

The same promises hold on POSIX and on Windows; test_localfs_windows.py exercises the
Windows calls with stand-ins on any platform.
"""

import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from proton_mail_mcp import localfs

CREATE = os.O_RDWR | os.O_CREAT


def opened(path, flags=CREATE, **at):
    return localfs.open(path, flags, 0o600, **at)


@pytest.fixture
def descriptors():
    held = []
    yield held
    for descriptor in held:
        try:
            os.close(descriptor)
        except OSError:
            pass


@pytest.fixture
def two(tmp_path, descriptors):
    """Two independent opens of one file."""
    path = tmp_path / "held.lock"
    descriptors += [opened(path), opened(path)]
    return descriptors


def test_an_exclusive_lock_refuses_every_other_open_until_it_closes(two):
    first, second = two
    localfs.lock(first)
    with pytest.raises(BlockingIOError):
        localfs.lock(second)
    with pytest.raises(BlockingIOError):
        localfs.lock(second, exclusive=False)
    os.close(first)
    two.remove(first)
    localfs.lock(second)


def test_shared_locks_share_and_refuse_an_exclusive_one(two, tmp_path):
    first, second = two
    localfs.lock(first, exclusive=False)
    localfs.lock(second, exclusive=False)
    third = opened(tmp_path / "held.lock")
    two.append(third)
    with pytest.raises(BlockingIOError):
        localfs.lock(third)
    for descriptor in (first, second):
        os.close(descriptor)
        two.remove(descriptor)
    localfs.lock(third)


def test_closing_a_duplicate_keeps_the_lock(two):
    # The action log reads through a duplicate while the original holds the lock.
    first, second = two
    localfs.lock(first)
    os.close(os.dup(first))
    with pytest.raises(BlockingIOError):
        localfs.lock(second)


def test_a_lock_conflicts_across_processes_and_ends_with_the_process(two, tmp_path):
    holder = (
        "import os, sys; from proton_mail_mcp import localfs\n"
        "descriptor = localfs.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "localfs.lock(descriptor)\nprint('held', flush=True)\nsys.stdin.read()\n"
    )
    first, _ = two
    process = subprocess.Popen(
        [sys.executable, "-c", holder, str(tmp_path / "held.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(BlockingIOError):
            localfs.lock(first)
    finally:
        process.stdin.close()
        process.wait(timeout=60)
        process.stdout.close()
    deadline = time.monotonic() + 10
    while True:
        try:
            localfs.lock(first)
            break
        except BlockingIOError:
            assert time.monotonic() < deadline
            time.sleep(0.05)


def test_a_lock_blocks_no_read_or_write_of_the_locked_file(two):
    # The action log locks the file that holds its records.
    first, second = two
    os.write(first, b"record\n")
    localfs.lock(first)
    os.lseek(first, 0, os.SEEK_SET)
    assert os.read(second, 7) == b"record\n"
    assert os.read(first, 7) == b"record\n"
    assert os.write(second, b"more\n") == 5
    os.close(second)
    two.remove(second)
    assert os.read(first, 5) == b"more\n"


def test_a_shared_lock_blocks_no_read_of_the_locked_file(two):
    first, second = two
    os.write(first, b"record\n")
    localfs.lock(first, exclusive=False)
    os.lseek(second, 0, os.SEEK_SET)
    assert os.read(second, 7) == b"record\n"


def test_bytes_stay_exact_through_a_descriptor(tmp_path, descriptors):
    # Windows descriptors default to text mode, which would break the journal's checksums.
    data = b"line\nwith\r\nend\x1a\x00tail\n"
    descriptor = opened(tmp_path / "bytes", os.O_RDWR | os.O_CREAT | os.O_APPEND)
    descriptors.append(descriptor)
    assert os.write(descriptor, data) == len(data)
    os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    assert os.read(descriptor, 100) == data
    assert (tmp_path / "bytes").read_bytes() == data


def test_append_writes_at_the_end_after_a_read(tmp_path, descriptors):
    descriptor = opened(tmp_path / "log", os.O_RDWR | os.O_CREAT | os.O_APPEND)
    descriptors.append(descriptor)
    os.write(descriptor, b"one\n")
    os.lseek(descriptor, 0, os.SEEK_SET)
    assert os.read(descriptor, 2) == b"on"
    os.write(descriptor, b"two\n")
    assert (tmp_path / "log").read_bytes() == b"one\ntwo\n"


def test_exclusive_creation_and_missing_files_raise_the_usual_errors(tmp_path, descriptors):
    path = tmp_path / "new"
    descriptors.append(opened(path, os.O_RDWR | os.O_CREAT | os.O_EXCL))
    with pytest.raises(FileExistsError):
        opened(path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
    with pytest.raises(FileNotFoundError):
        opened(tmp_path / "missing", os.O_RDONLY)
    with pytest.raises(FileNotFoundError):
        opened(tmp_path / "no-such-folder" / "file")


def test_a_directory_opens_only_with_o_directory_or_as_itself(tmp_path, descriptors):
    (tmp_path / "file").write_bytes(b"")
    with pytest.raises(NotADirectoryError):
        opened(tmp_path / "file", os.O_RDONLY | localfs.O_DIRECTORY)
    descriptor = opened(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY | localfs.O_NOFOLLOW)
    descriptors.append(descriptor)
    assert stat.S_ISDIR(os.fstat(descriptor).st_mode)


def test_names_in_an_open_directory(tmp_path, descriptors):
    (tmp_path / "folder").mkdir()
    (tmp_path / "other").write_bytes(b"not this one")
    directory = opened(tmp_path / "folder", os.O_RDONLY | localfs.O_DIRECTORY)
    descriptors.append(directory)
    descriptor = localfs.open("a", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory)
    os.write(descriptor, b"a")
    os.close(descriptor)
    assert (tmp_path / "folder" / "a").read_bytes() == b"a"
    info = localfs.stat_at("a", dir_fd=directory, follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode) and info.st_size == 1
    localfs.mkdir("sub", 0o700, dir_fd=directory)
    assert (tmp_path / "folder" / "sub").is_dir()
    same = opened(".", os.O_RDONLY | localfs.O_DIRECTORY, dir_fd=directory)
    descriptors.append(same)
    assert os.fstat(same).st_ino == os.fstat(directory).st_ino
    localfs.replace("a", "b", src_dir_fd=directory, dst_dir_fd=directory)
    assert (tmp_path / "folder" / "b").read_bytes() == b"a"
    localfs.unlink("b", dir_fd=directory)
    assert not (tmp_path / "folder" / "b").exists()
    with pytest.raises(FileNotFoundError):
        localfs.unlink("b", dir_fd=directory)
    localfs.fsync_directory(directory)
    assert (tmp_path / "other").read_bytes() == b"not this one"


def test_a_locked_file_can_be_removed_with_its_lock_held(tmp_path):
    # SearchIndex.discard removes its lock file while holding that lock.
    path = tmp_path / "lock"
    descriptor = opened(path)
    localfs.lock(descriptor)
    localfs.unlink(path)
    os.close(descriptor)
    assert not path.exists()


def test_a_rename_over_a_file_waits_out_a_brief_reader(tmp_path):
    # Saved rules readers take no lock, relying on the rename being atomic.
    path, temporary = tmp_path / "store", tmp_path / "temporary"
    path.write_bytes(b"old")
    temporary.write_bytes(b"new")
    reader = opened(path, os.O_RDONLY)
    threading.Timer(0.2, os.close, [reader]).start()
    localfs.replace(temporary, path)
    assert path.read_bytes() == b"new"
    time.sleep(0.25)


def test_fchmod_leaves_a_file_usable(tmp_path, descriptors):
    descriptor = opened(tmp_path / "file")
    descriptors.append(descriptor)
    localfs.fchmod(descriptor, 0o600)
    assert os.write(descriptor, b"x") == 1


@pytest.fixture
def link_to(tmp_path):
    """A symbolic link; Windows needs a privilege or Developer Mode to make one."""

    def make(target, *, directory=False):
        link = tmp_path / f"link-{target.name}"
        try:
            os.symlink(target, link, target_is_directory=directory)
        except OSError as error:
            pytest.skip(f"cannot create a symbolic link here: {error}")
        # Wine's CreateSymbolicLinkW reports success and makes nothing.
        if not os.path.lexists(link):
            pytest.skip("this system cannot make symbolic links")
        return link

    return make


def test_o_nofollow_refuses_a_link_to_a_file_and_a_dangling_one(tmp_path, link_to):
    target = tmp_path / "target"
    target.write_bytes(b"private")
    link = link_to(target)
    for flags in (os.O_RDONLY, CREATE):
        with pytest.raises(OSError):
            opened(link, flags | localfs.O_NOFOLLOW)
    target.unlink()
    with pytest.raises(OSError):
        opened(link, CREATE | localfs.O_NOFOLLOW)
    assert not target.exists()
    with pytest.raises(FileExistsError):
        opened(link, CREATE | os.O_EXCL | localfs.O_NOFOLLOW)
    assert not target.exists()


def test_o_nofollow_refuses_a_link_to_a_directory(tmp_path, link_to):
    target = tmp_path / "folder"
    target.mkdir()
    link = link_to(target, directory=True)
    with pytest.raises(OSError):
        opened(link, os.O_RDONLY | localfs.O_DIRECTORY | localfs.O_NOFOLLOW)


def test_without_o_nofollow_a_directory_link_is_followed(tmp_path, link_to, descriptors):
    # As rules_timer does for the state file's folder.
    target = tmp_path / "folder"
    target.mkdir()
    link = link_to(target, directory=True)
    descriptor = opened(link, os.O_RDONLY | localfs.O_DIRECTORY)
    descriptors.append(descriptor)
    assert os.fstat(descriptor).st_ino == os.stat(target).st_ino


windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows behaviour")
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
# Wine derives access rules from Unix modes, and rules set on a file do not stay.
real_rules = pytest.mark.skipif(
    sys.platform != "win32" or hasattr(__import__("ctypes").WinDLL("ntdll"), "wine_get_version"),
    reason="needs real Windows access rules",
)


@windows
def test_o_nofollow_refuses_a_junction(tmp_path):
    import _winapi

    target = tmp_path / "folder"
    target.mkdir()
    (target / "file").write_bytes(b"private")
    junction = tmp_path / "junction"
    try:
        _winapi.CreateJunction(str(target), str(junction))
    except OSError as error:  # Wine has none.
        pytest.skip(f"cannot create a junction here: {error}")
    with pytest.raises(OSError):
        opened(junction, os.O_RDONLY | localfs.O_DIRECTORY | localfs.O_NOFOLLOW)


@windows
def test_an_open_directory_cannot_be_renamed_or_removed(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    descriptor = opened(folder, os.O_RDONLY | localfs.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError):
            os.rename(folder, tmp_path / "moved")
        with pytest.raises(OSError):
            os.rmdir(folder)
    finally:
        os.close(descriptor)
    os.rename(folder, tmp_path / "moved")


@windows
def test_reparse_and_file_name_rules_on_a_real_windows_path(tmp_path):
    assert localfs.profile_root(tmp_path)
    assert localfs.state_home().parts[-2:] == ("proton-mail-mcp", "state")
    assert localfs.reserved_name("nul.txt") and localfs.reserved_name("a:b")
    assert not localfs.reserved_name("report.pdf")
    assert localfs.plain_name("con.txt") == "_con.txt"


def set_access_rules(path, rules):
    """Replace a path's rules, written in SDDL; the owner keeps access so the test can clean up."""
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_void_p]
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        rules, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not advapi32.SetFileSecurityW(str(path), 4, descriptor):  # DACL_SECURITY_INFORMATION
        raise ctypes.WinError(ctypes.get_last_error())


PRIVATE_RULES = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"


@windows
def test_the_real_api_reads_a_new_private_folder_as_private(tmp_path, descriptors):
    folder = tmp_path / "private"
    os.mkdir(folder, 0o700)
    descriptors.append(localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY))
    assert localfs.acl_refusal(descriptors[-1]) is None
    assert localfs._my_sid().startswith("S-1-5-")


@real_rules
def test_real_private_rules_pass_and_a_real_stranger_refuses(tmp_path, descriptors):
    path = tmp_path / "file"
    path.touch()
    set_access_rules(path, PRIVATE_RULES)
    descriptors.append(opened(path))
    assert localfs.acl_refusal(descriptors[-1]) is None
    for stranger, named in (("WD", "Everyone"), ("BU", "Users"), ("AU", "Authenticated Users")):
        set_access_rules(path, f"{PRIVATE_RULES}(A;;FR;;;{stranger})")
        descriptors.append(opened(path))
        assert named in localfs.acl_refusal(descriptors[-1])
    set_access_rules(path, f"{PRIVATE_RULES}(A;;FA;;;S-1-5-21-1111-2222-3333-1002)")
    descriptors.append(opened(path))
    assert "another account" in localfs.acl_refusal(descriptors[-1])


@real_rules
def test_a_real_null_dacl_refuses(tmp_path, descriptors):
    path = tmp_path / "file"
    path.touch()
    set_access_rules(path, "D:NO_ACCESS_CONTROL")
    descriptors.append(opened(path))
    assert localfs.acl_refusal(descriptors[-1])


@real_rules
def test_a_real_shared_folder_in_a_private_tree_refuses(tmp_path, descriptors):
    folder = tmp_path / "shared"
    folder.mkdir()
    set_access_rules(folder, f"{PRIVATE_RULES}(A;OICI;FR;;;BU)")
    descriptors.append(localfs.open(folder, os.O_RDONLY | localfs.O_DIRECTORY))
    assert "Users" in localfs.acl_refusal(descriptors[-1])
    if localfs.profile_root(tmp_path):
        assert f"{folder.name} gives access to Users" in localfs.path_refusal(folder / "log")


@real_rules
def test_local_app_data_redirected_to_a_real_shared_folder_refuses(tmp_path, monkeypatch):
    place = tmp_path / "shared"
    place.mkdir()
    set_access_rules(place, f"{PRIVATE_RULES}(A;OICI;FR;;;BU)")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    monkeypatch.setenv("LOCALAPPDATA", str(place))
    assert "gives access to Users" in localfs.path_refusal(localfs.state_home())


@posix
def test_posix_checks_are_the_ones_the_callers_made(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"")
    os.chmod(path, 0o600)
    assert localfs.owned(path.stat()) and localfs.mode_private(path.stat())
    os.chmod(path, 0o640)
    assert not localfs.mode_private(path.stat())
    assert not localfs.world_writable(path.stat())
    os.chmod(path, 0o602)
    assert localfs.world_writable(path.stat())
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    assert not localfs.owned(path.stat())


@posix
def test_posix_state_home_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert localfs.state_home() == tmp_path / "proton-mail-mcp"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert localfs.state_home() == tmp_path / ".local" / "state" / "proton-mail-mcp"


@posix
def test_posix_file_names_are_never_reserved():
    for name in ("con", "NUL.txt", "a:b", "trailing.", "x~1"):
        assert not localfs.reserved_name(name)
        assert localfs.plain_name(name) == name
