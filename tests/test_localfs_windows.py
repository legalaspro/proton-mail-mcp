"""The Windows branches of the file seam, on any platform, with stand-ins for Windows itself.

A stand-in kernel32 and msvcrt turn the calls into POSIX ones on real temporary files. That
checks what this code asks of Windows, not what Windows does: test_localfs.py does that where
Windows, or Wine, is really there. The last tests here check that POSIX still gets its exact calls.
"""

import ast
import ctypes
import errno
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from proton_mail_mcp import localfs

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "proton_mail_mcp"
# conftest forbids subprocess.run during tests; these tests run local Python only.
REAL_RUN = subprocess.run

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
SHARE_READ = 1
SHARE_WRITE = 2
SHARE_DELETE = 4
CREATE_NEW = 1
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
ATTRIBUTE_NORMAL = 0x80
BACKUP_SEMANTICS = 0x02000000
OPEN_REPARSE_POINT = 0x00200000
FAIL_IMMEDIATELY = 1
EXCLUSIVE = 2
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_EXISTS = 80
ERROR_IO_PENDING = 997
ERRNO_OF = {
    ERROR_FILE_NOT_FOUND: errno.ENOENT,
    ERROR_ACCESS_DENIED: errno.EACCES,
    ERROR_INVALID_HANDLE: errno.EBADF,
    ERROR_FILE_EXISTS: errno.EEXIST,
}
INVALID_HANDLE = ctypes.c_void_p(-1).value
# Opens a link without following it, on Linux and on macOS.
LINK_ITSELF = getattr(os, "O_PATH", getattr(os, "O_SYMLINK", 0))
O_BINARY = 0x8000


ME = "S-1-5-21-1111-2222-3333-1001"
OTHER_USER = "S-1-5-21-1111-2222-3333-1002"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
OWNER_RIGHTS = "S-1-3-4"
CREATOR_OWNER = "S-1-3-0"
EVERYONE = "S-1-1-0"
USERS = "S-1-5-32-545"
AUTHENTICATED = "S-1-5-11"
# What a real Windows Server 2025 runner's profile folder carried, read by these checks in CI.
CAPABILITY = (
    "S-1-15-3-65536-599108337-2355189375-1353122160-3480128286-3345335107-485756383-4087318168-"
    "230526575"
)
ALLOW = 0
DENY = 1
OBJECT_INHERIT = 0x01
CONTAINER_INHERIT = 0x02
INHERIT_ONLY = 0x08
INHERITED = 0x10
FULL = 0x001F01FF
READ = 0x00120089
MODIFY = 0x001301BF


def sid_bytes(text):
    """A SID as Windows lays it out: authority big-endian, sub-authorities little-endian."""
    _, revision, authority, *subs = text.split("-")
    return (
        bytes([int(revision), len(subs)])
        + int(authority).to_bytes(6, "big")
        + struct.pack(f"<{len(subs)}I", *map(int, subs))
    )


def ace(who, mask=FULL, flags=0, kind=ALLOW):
    """One ACCESS_ALLOWED_ACE or ACCESS_DENIED_ACE, as Windows lays it out."""
    body = struct.pack("<I", mask) + sid_bytes(who)
    return struct.pack("<BBH", kind, flags, 4 + len(body)) + body


def acl(*aces):
    """An ACL header and its ACEs, as Windows lays them out."""
    body = b"".join(aces)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body


def owner_only():
    return ME, acl(ace(ME))


def python_0700():
    """What Python's mkdir(0o700) gave on a real Windows Server runner, by icacls."""
    inherit = OBJECT_INHERIT | CONTAINER_INHERIT
    return ME, acl(
        ace(SYSTEM, flags=inherit),
        ace(ADMINISTRATORS, flags=inherit),
        ace(OWNER_RIGHTS, flags=inherit),
    )


def profile_folder():
    """What %LOCALAPPDATA% showed on that runner, by icacls, inherited as it is below a profile."""
    inherit = OBJECT_INHERIT | CONTAINER_INHERIT | INHERITED
    return ME, acl(
        ace(SYSTEM, flags=inherit), ace(ADMINISTRATORS, flags=inherit), ace(ME, flags=inherit)
    )


def shared(who=EVERYONE, mask=READ, flags=0):
    return ME, acl(ace(ME), ace(SYSTEM), ace(ADMINISTRATORS), ace(who, mask, flags))


class Security:
    """What GetSecurityInfo reports, in the bytes Windows would hand back.

    A rule set on a path follows that file or folder when it moves, and what has none of its own
    takes the rules of the nearest folder above it that has, as a new file inherits them.
    """

    def __init__(self):
        self.of_file = {}
        self.of_folder = {}
        self.calls = []
        self.freed = []
        self.error = 0
        self.token_error = 0
        self.kept = []
        self.user = ME

    def set(self, path, rules):
        owner, dacl = rules
        info = os.stat(path)
        self.of_file[(info.st_dev, info.st_ino)] = rules
        if stat.S_ISDIR(info.st_mode):
            self.of_folder[os.path.realpath(path)] = rules
        return owner, dacl

    def rules(self, handle, known_path):
        info = os.fstat(handle)
        own = self.of_file.get((info.st_dev, info.st_ino))
        if own is not None:
            return own
        folders = [
            folder
            for folder in self.of_folder
            if known_path and known_path.startswith(folder + os.sep)
        ]
        return self.of_folder[max(folders, key=len)] if folders else owner_only()

    def memory(self, data):
        buffer = ctypes.create_string_buffer(data, len(data))
        self.kept.append(buffer)
        return ctypes.addressof(buffer)


class Function:
    """A kernel32 function that takes the argtypes ctypes would."""

    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class Kernel32:
    """Enough of kernel32 to run localfs on real temporary files."""

    def __init__(self):
        self.opened = []
        self.paths = {}
        self.final_path_calls = 0
        self.locks = {}
        self.region = None
        self.lock_flags = []
        self.lock_error = None
        self.final_path_error = None
        self.open_error = None
        self.closed_handles = []
        self.last_error = 0
        self.security = Security()
        self.GetSecurityInfo = Function(self._security_info)
        self.GetCurrentProcess = Function(lambda: 0xFFFFFFFF)
        self.LocalFree = Function(self._local_free)
        self.OpenProcessToken = Function(self._open_token)
        self.GetTokenInformation = Function(self._token_information)
        self.CreateFileW = Function(self._create)
        self.LockFileEx = Function(self._lock)
        self.GetFinalPathNameByHandleW = Function(self._final_path)
        self.CloseHandle = Function(self._close)

    def _close(self, handle):
        handle = getattr(handle, "value", handle)
        self.closed_handles.append(handle)
        os.close(handle)
        return True

    def _security_info(self, handle, kind, wanted, owner, group, dacl, sacl, memory):
        security = self.security
        security.calls.append((handle, kind, wanted, group, sacl))
        if security.error:
            return security.error
        path = self.paths.get(handle)
        who, rules = security.rules(handle, path)
        owner._obj.value = security.memory(sid_bytes(who)) if who else None
        dacl._obj.value = security.memory(rules) if rules is not None else None
        memory._obj.value = security.memory(b"descriptor")
        return 0

    def _local_free(self, memory):
        self.security.freed.append(memory.value)

    def _open_token(self, process, access, token):
        assert access == 8  # TOKEN_QUERY
        if self.security.token_error:
            self.last_error = self.security.token_error
            return False
        token._obj.value = os.open(os.devnull, os.O_RDONLY)
        return True

    def _token_information(self, token, kind, buffer, size, needed):
        assert kind == 1  # TokenUser
        sid = self.security.memory(sid_bytes(self.security.user))
        struct.pack_into("<Q", buffer, 0, sid)
        return True

    def _create(self, name, access, share, security, disposition, attributes, template):
        self.opened.append(
            SimpleNamespace(
                name=name,
                access=access,
                share=share,
                security=security,
                disposition=disposition,
                attributes=attributes,
                template=template,
            )
        )
        if self.open_error is not None:
            self.last_error = self.open_error
            return INVALID_HANDLE
        reading, writing = bool(access & GENERIC_READ), bool(access & GENERIC_WRITE)
        flags = {(True, False): os.O_RDONLY, (False, True): os.O_WRONLY}.get(
            (reading, writing), os.O_RDWR
        )
        if disposition == CREATE_NEW:
            flags |= os.O_CREAT | os.O_EXCL
        elif disposition == OPEN_ALWAYS:
            flags |= os.O_CREAT
        try:
            if attributes & OPEN_REPARSE_POINT and os.path.islink(name):
                if disposition == CREATE_NEW:
                    raise FileExistsError
                # The link itself, as FILE_FLAG_OPEN_REPARSE_POINT opens it.
                descriptor = os.open(name, LINK_ITSELF | os.O_NOFOLLOW)
            elif os.path.isdir(name) and not attributes & BACKUP_SEMANTICS:
                raise PermissionError
            else:
                descriptor = os.open(name, flags, 0o600)
        except FileNotFoundError:
            self.last_error = ERROR_FILE_NOT_FOUND
            return INVALID_HANDLE
        except FileExistsError:
            self.last_error = ERROR_FILE_EXISTS
            return INVALID_HANDLE
        except PermissionError:
            self.last_error = ERROR_ACCESS_DENIED
            return INVALID_HANDLE
        self.paths[descriptor] = os.path.realpath(name)
        return descriptor

    def _lock(self, handle, flags, reserved, low, high, overlapped):
        self.region = overlapped._obj
        self.lock_flags.append((flags, reserved, low, high))
        if self.lock_error is not None:
            self.last_error = self.lock_error
            return False
        info = os.fstat(handle)
        holders = self.locks.setdefault((info.st_dev, info.st_ino), [])
        if holders and (flags & EXCLUSIVE or any(kind & EXCLUSIVE for kind in holders)):
            self.last_error = ERROR_LOCK_VIOLATION
            return False
        holders.append(flags)
        return True

    def _final_path(self, handle, buffer, size, flags):
        self.final_path_calls += 1
        assert flags == 0  # VOLUME_NAME_DOS
        if self.final_path_error is not None:
            self.last_error = self.final_path_error
            return 0
        path = self.paths[handle]
        if len(path) >= size:
            return len(path) + 1
        buffer.value = path
        return len(path)


class Stat:
    """A POSIX stat result with the attributes Windows' has."""

    def __init__(self, info):
        self.info = info
        self.st_file_attributes = (
            stat.FILE_ATTRIBUTE_REPARSE_POINT if stat.S_ISLNK(info.st_mode) else 0
        )

    def __getattr__(self, name):
        return getattr(self.info, name)


@pytest.fixture
def kernel32(monkeypatch):
    fake = Kernel32()
    real_fstat = os.fstat
    localfs._kernel32.cache_clear()
    localfs._advapi32.cache_clear()
    localfs._my_sid.cache_clear()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(localfs, "fcntl", None)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error: fake, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: fake.last_error, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda code: OSError(ERRNO_OF[code], f"WinError {code}"),
        raising=False,
    )
    monkeypatch.setattr(os, "O_BINARY", O_BINARY, raising=False)
    monkeypatch.setattr(os, "fstat", lambda descriptor: Stat(real_fstat(descriptor)))
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            get_osfhandle=lambda descriptor: descriptor,
            open_osfhandle=lambda handle, flags: (
                fake.__dict__.setdefault("crt_flags", []).append(flags) or handle
            ),
        ),
    )
    yield fake
    localfs._kernel32.cache_clear()
    localfs._advapi32.cache_clear()
    localfs._my_sid.cache_clear()


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the stand-ins are POSIX calls; test_localfs.py runs here"
)


# Locks ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exclusive,flags",
    [(True, FAIL_IMMEDIATELY | EXCLUSIVE), (False, FAIL_IMMEDIATELY)],
)
def test_lock_asks_for_one_byte_past_any_data_without_waiting(kernel32, tmp_path, exclusive, flags):
    descriptor = localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)
    localfs.lock(descriptor, exclusive=exclusive)
    assert kernel32.lock_flags == [(flags, 0, 1, 0)]
    # Mandatory Windows locks would otherwise block other handles' reads.
    assert kernel32.region.OffsetHigh << 32 >= 2**62 and kernel32.region.Offset == 0
    os.close(descriptor)


def test_a_held_lock_raises_what_every_caller_catches(kernel32, tmp_path):
    first = localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)
    second = localfs.open(tmp_path / "file", os.O_RDWR)
    localfs.lock(first)
    with pytest.raises(BlockingIOError):
        localfs.lock(second)
    with pytest.raises(BlockingIOError):
        localfs.lock(second, exclusive=False)
    os.close(first)
    os.close(second)


@pytest.mark.parametrize("error", [ERROR_LOCK_VIOLATION, ERROR_IO_PENDING])
def test_both_would_block_errors_mean_busy(kernel32, tmp_path, error):
    descriptor = localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)
    kernel32.lock_error = error
    with pytest.raises(BlockingIOError):
        localfs.lock(descriptor)
    os.close(descriptor)


def test_any_other_lock_failure_is_not_busy(kernel32, tmp_path):
    descriptor = localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)
    kernel32.lock_error = ERROR_INVALID_HANDLE
    with pytest.raises(OSError) as caught:
        localfs.lock(descriptor)
    assert not isinstance(caught.value, BlockingIOError)
    os.close(descriptor)


# Opening ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags,access,disposition",
    [
        (os.O_RDONLY, GENERIC_READ, OPEN_EXISTING),
        (os.O_WRONLY | os.O_CREAT, GENERIC_WRITE, OPEN_ALWAYS),
        (os.O_RDWR, GENERIC_READ | GENERIC_WRITE, OPEN_EXISTING),
        (os.O_RDWR | os.O_CREAT, GENERIC_READ | GENERIC_WRITE, OPEN_ALWAYS),
        (os.O_WRONLY | os.O_CREAT | os.O_EXCL, GENERIC_WRITE, CREATE_NEW),
        (os.O_RDONLY | localfs.O_NONBLOCK | localfs.O_CLOEXEC, GENERIC_READ, OPEN_EXISTING),
    ],
)
def test_flags_become_create_file_arguments(kernel32, tmp_path, flags, access, disposition):
    path = tmp_path / "file"
    if disposition != CREATE_NEW:
        path.write_bytes(b"")
    os.close(localfs.open(path, flags, 0o600))
    [call] = kernel32.opened
    assert call.name == str(path)
    assert (call.access, call.disposition) == (access, disposition)
    # Renamed over and removed while open, as on POSIX; no inherited handle, no template.
    assert call.share == SHARE_READ | SHARE_WRITE | SHARE_DELETE
    assert call.attributes == ATTRIBUTE_NORMAL
    assert call.security is None and call.template is None


def test_a_descriptor_never_translates_newlines_and_keeps_o_append(kernel32, tmp_path):
    os.close(localfs.open(tmp_path / "plain", os.O_RDWR | os.O_CREAT))
    os.close(localfs.open(tmp_path / "log", os.O_RDWR | os.O_CREAT | os.O_APPEND))
    assert kernel32.crt_flags == [O_BINARY, O_BINARY | os.O_APPEND]


def test_o_nofollow_opens_the_link_itself(kernel32, tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"")
    os.close(localfs.open(path, os.O_RDONLY | localfs.O_NOFOLLOW))
    assert kernel32.opened[0].attributes == ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_RDWR | os.O_CREAT])
def test_a_link_or_junction_is_refused_and_its_handle_closed(
    kernel32, tmp_path, flags, monkeypatch
):
    target = tmp_path / "target"
    target.write_bytes(b"private")
    link = tmp_path / "link"
    link.symlink_to(target)
    closed = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    with pytest.raises(OSError) as caught:
        localfs.open(link, flags | localfs.O_NOFOLLOW)
    assert caught.value.errno == errno.ELOOP
    assert len(closed) == 1
    assert target.read_bytes() == b"private"


def test_without_o_nofollow_a_link_is_followed(kernel32, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"data")
    link = tmp_path / "link"
    link.symlink_to(target)
    descriptor = localfs.open(link, os.O_RDONLY)
    assert os.read(descriptor, 4) == b"data"
    os.close(descriptor)


def test_a_new_name_where_a_link_lies_is_refused(kernel32, tmp_path):
    target = tmp_path / "target"
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        localfs.open(link, os.O_WRONLY | os.O_CREAT | os.O_EXCL | localfs.O_NOFOLLOW)
    assert not target.exists()


def test_a_directory_stays_in_place_while_its_descriptor_lasts(kernel32, tmp_path):
    descriptor = localfs.open(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY | localfs.O_NOFOLLOW)
    [call] = kernel32.opened
    # Without FILE_SHARE_DELETE; read access makes Windows hold it to that share mode.
    assert call.share == SHARE_READ | SHARE_WRITE
    assert call.access == GENERIC_READ
    assert call.attributes == ATTRIBUTE_NORMAL | BACKUP_SEMANTICS | OPEN_REPARSE_POINT
    assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
    os.close(descriptor)


def test_o_directory_refuses_a_file_and_closes_it(kernel32, tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"")
    closed = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    with pytest.raises(NotADirectoryError):
        localfs.open(path, os.O_RDONLY | localfs.O_DIRECTORY)
    assert len(closed) == 1


def test_a_directory_needs_o_directory_to_open(kernel32, tmp_path):
    with pytest.raises(PermissionError):
        localfs.open(tmp_path, os.O_RDONLY)


@pytest.mark.parametrize(
    "flags,error",
    [
        (os.O_RDONLY, FileNotFoundError),
        (os.O_WRONLY | os.O_CREAT | os.O_EXCL, FileExistsError),
    ],
)
def test_failures_raise_the_usual_errors(kernel32, tmp_path, flags, error):
    path = tmp_path / "file"
    if error is FileExistsError:
        path.write_bytes(b"")
    with pytest.raises(error):
        localfs.open(path, flags)


def test_a_file_another_program_holds_for_now_is_busy_not_refused(kernel32, tmp_path):
    # SearchIndex turns itself off until a restart on a lasting refusal.
    kernel32.open_error = ERROR_SHARING_VIOLATION
    with pytest.raises(BlockingIOError):
        localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)


def test_a_handle_the_c_runtime_refuses_is_closed(kernel32, tmp_path, monkeypatch):
    def full(handle, flags):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(sys.modules["msvcrt"], "open_osfhandle", full)
    with pytest.raises(OSError):
        localfs.open(tmp_path / "file", os.O_RDWR | os.O_CREAT)
    assert len(kernel32.closed_handles) == 1


# Names in an open directory ---------------------------------------------------


def test_a_name_in_an_open_directory_uses_that_directorys_real_path(kernel32, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    directory = localfs.open(link, os.O_RDONLY | localfs.O_DIRECTORY)
    os.close(localfs.open("name", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=directory))
    assert (real / "name").exists()
    assert kernel32.opened[-1].name == str(real / "name")
    os.close(directory)


def test_dot_names_the_open_directory_itself(kernel32, tmp_path):
    directory = localfs.open(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY)
    again = localfs.open(".", os.O_RDONLY | localfs.O_DIRECTORY, dir_fd=directory)
    assert kernel32.opened[-1].name == str(tmp_path)
    os.close(again)
    os.close(directory)


def test_a_long_directory_path_is_asked_for_again(kernel32, tmp_path):
    directory = localfs.open(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY)
    kernel32.paths[directory] = "/" + "x" * 2000
    assert localfs._at("name", directory) == "/" + "x" * 2000 + "/name"
    assert kernel32.final_path_calls == 2
    os.close(directory)


def test_a_directory_path_windows_cannot_give_refuses(kernel32, tmp_path):
    directory = localfs.open(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY)
    kernel32.final_path_error = ERROR_INVALID_HANDLE
    with pytest.raises(OSError):
        localfs.open("name", os.O_RDWR | os.O_CREAT, dir_fd=directory)
    os.close(directory)


def test_every_name_call_takes_its_directory_from_the_descriptor(kernel32, tmp_path):
    directory = localfs.open(tmp_path, os.O_RDONLY | localfs.O_DIRECTORY)
    os.close(localfs.open("a", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory))
    info = localfs.stat_at("a", dir_fd=directory, follow_symlinks=False)
    assert stat.S_ISREG(info.st_mode)
    localfs.replace("a", "b", src_dir_fd=directory, dst_dir_fd=directory)
    assert (tmp_path / "b").exists()
    localfs.mkdir("sub", 0o700, dir_fd=directory)
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700
    localfs.unlink("b", dir_fd=directory)
    assert not (tmp_path / "b").exists()
    os.close(directory)


def test_calls_without_a_directory_take_the_path_as_given(kernel32, tmp_path):
    os.close(localfs.open(tmp_path / "a", os.O_WRONLY | os.O_CREAT))
    assert stat.S_ISREG(localfs.stat_at(tmp_path / "a").st_mode)
    localfs.replace(tmp_path / "a", tmp_path / "b")
    localfs.mkdir(tmp_path / "sub")
    localfs.unlink(tmp_path / "b")
    assert (tmp_path / "sub").is_dir() and not (tmp_path / "b").exists()


@pytest.mark.parametrize("call", ["replace", "unlink"])
def test_a_brief_holder_is_waited_out(kernel32, tmp_path, monkeypatch, call):
    (tmp_path / "a").write_bytes(b"")
    real = getattr(os, call)
    tries = []

    def busy(*args, **kwargs):
        tries.append(args)
        if len(tries) < 3:
            raise PermissionError
        return real(*args, **kwargs)

    pauses = []
    monkeypatch.setattr(os, call, busy)
    monkeypatch.setattr(localfs.time, "sleep", pauses.append)
    if call == "replace":
        localfs.replace(tmp_path / "a", tmp_path / "b")
    else:
        localfs.unlink(tmp_path / "a")
    assert len(tries) == 3 and len(pauses) == 2


@pytest.mark.parametrize("call", ["replace", "unlink"])
def test_a_holder_that_stays_refuses_within_a_second(kernel32, tmp_path, monkeypatch, call):
    tries = []

    def busy(*args, **kwargs):
        tries.append(args)
        raise PermissionError

    pauses = []
    monkeypatch.setattr(os, call, busy)
    monkeypatch.setattr(localfs.time, "sleep", pauses.append)
    with pytest.raises(PermissionError):
        if call == "replace":
            localfs.replace(tmp_path / "a", tmp_path / "b")
        else:
            localfs.unlink(tmp_path / "a")
    assert len(tries) == localfs._BUSY_TRIES
    assert sum(pauses) <= 1


def test_other_failures_are_not_retried(kernel32, tmp_path, monkeypatch):
    monkeypatch.setattr(localfs.time, "sleep", lambda _: pytest.fail("slept"))
    with pytest.raises(FileNotFoundError):
        localfs.unlink(tmp_path / "missing")


# What Windows does not have ---------------------------------------------------


def test_directory_fsync_and_fchmod_do_nothing(kernel32, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda _: pytest.fail("fsync"))
    monkeypatch.setattr(os, "fchmod", lambda *_: pytest.fail("fchmod"), raising=False)
    localfs.fsync_directory(3)
    localfs.fchmod(3, 0o600)


def test_owner_and_mode_checks_pass_with_windows_stat_values(kernel32, monkeypatch):
    monkeypatch.delattr(os, "getuid")
    info = SimpleNamespace(st_uid=0, st_mode=0o100666)
    assert localfs.owned(info) and localfs.mode_private(info)
    assert not localfs.world_writable(SimpleNamespace(st_mode=0o40777))


# Where things live ------------------------------------------------------------


def test_state_lives_in_local_app_data_whatever_xdg_says(kernel32, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", "/elsewhere")
    assert localfs.state_home() == tmp_path / "proton-mail-mcp" / "state"


@pytest.mark.parametrize("value", [None, "", "relative"])
def test_without_a_usable_local_app_data_state_lives_in_the_home(kernel32, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LOCALAPPDATA")
    else:
        monkeypatch.setenv("LOCALAPPDATA", value)
    expected = Path.home() / "AppData" / "Local" / "proton-mail-mcp" / "state"
    assert localfs.state_home() == expected


@pytest.fixture
def profile(kernel32, monkeypatch, tmp_path):
    home = tmp_path / "Users" / "me"
    (home / "AppData" / "Local").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda _path: str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    return home


def test_paths_in_the_profile_count_and_others_do_not(profile, tmp_path, monkeypatch):
    assert localfs.profile_root(profile / "Documents" / "not-there-yet")
    assert localfs.profile_root(profile)
    assert not localfs.profile_root(tmp_path / "Users" / "other" / "file")
    assert not localfs.profile_root(tmp_path / "Users" / "me2" / "file")
    moved = tmp_path / "D-drive" / "Local"
    moved.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(moved))
    assert localfs.profile_root(moved / "proton-mail-mcp" / "state" / "actions.jsonl")


def test_a_link_out_of_the_profile_does_not_count(profile, tmp_path):
    outside = tmp_path / "shared"
    outside.mkdir()
    (profile / "link").symlink_to(outside)
    assert not localfs.profile_root(profile / "link" / "actions.jsonl")


def test_a_relative_local_app_data_gives_no_root(profile, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    monkeypatch.chdir(tmp_path)
    assert not localfs.profile_root(tmp_path / "relative" / "file")


def test_a_path_that_cannot_resolve_does_not_count(profile, monkeypatch):
    def broken(_path):
        raise OSError

    monkeypatch.setattr(os.path, "realpath", broken)
    assert not localfs.profile_root(profile / "file")


@pytest.mark.parametrize(
    "name",
    [
        "CON",
        "con",
        "nul.txt",
        "NUL .tar.gz",
        "aux",
        "COM1",
        "lpt9.log",
        "COM\xb9",
        "CONIN$",
        "report.pdf:hidden",
        "C:evil.exe",
        "a<b",
        "a>b",
        'a"b',
        "a|b",
        "a?b",
        "a*b",
        "tab\tname",
        "trailing.",
        "trailing ",
        "ABCDEF~1.TXT",
    ],
)
def test_names_windows_reads_as_something_else(kernel32, name):
    assert localfs.reserved_name(name)
    plain = localfs.plain_name(name)
    assert not localfs.reserved_name(plain) and plain


@pytest.mark.parametrize(
    "name",
    ["report.pdf", "console.log", "nullable", "COM10", "a b.txt", "x~y.txt", "ünï.txt", "COM"],
)
def test_ordinary_names_stay_as_they_are(kernel32, name):
    assert not localfs.reserved_name(name)
    assert localfs.plain_name(name) == name


# POSIX keeps its exact calls --------------------------------------------------


@pytest.mark.parametrize(
    "exclusive,operation",
    [(True, "LOCK_EX"), (False, "LOCK_SH")],
)
def test_posix_locks_are_the_same_flock_calls(monkeypatch, exclusive, operation):
    import fcntl

    calls = []
    monkeypatch.setattr(fcntl, "flock", lambda *args: calls.append(args))
    localfs.lock(7, exclusive=exclusive)
    assert calls == [(7, getattr(fcntl, operation) | fcntl.LOCK_NB)]


def test_posix_flags_are_the_os_values():
    for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"):
        assert getattr(localfs, name) == getattr(os, name)


@pytest.mark.parametrize(
    "call,args,kwargs,target,expected",
    [
        ("open", ("p", 1, 0o600), {}, "open", (("p", 1, 0o600), {})),
        ("open", ("p", 1), {"dir_fd": 4}, "open", (("p", 1, 0o777), {"dir_fd": 4})),
        (
            "stat_at",
            ("p",),
            {"dir_fd": 4, "follow_symlinks": False},
            "stat",
            (("p",), {"dir_fd": 4, "follow_symlinks": False}),
        ),
        ("unlink", ("p",), {"dir_fd": 4}, "unlink", (("p",), {"dir_fd": 4})),
        ("unlink", ("p",), {}, "unlink", (("p",), {})),
        ("replace", ("a", "b"), {}, "replace", (("a", "b"), {})),
        (
            "replace",
            ("a", "b"),
            {"src_dir_fd": 4, "dst_dir_fd": 5},
            "replace",
            (("a", "b"), {"src_dir_fd": 4, "dst_dir_fd": 5}),
        ),
        ("mkdir", ("p", 0o700), {"dir_fd": 4}, "mkdir", (("p", 0o700), {"dir_fd": 4})),
        ("fsync_directory", (4,), {}, "fsync", ((4,), {})),
        ("fchmod", (4, 0o600), {}, "fchmod", ((4, 0o600), {})),
    ],
)
def test_posix_calls_reach_os_unchanged(monkeypatch, call, args, kwargs, target, expected):
    seen = []
    monkeypatch.setattr(os, target, lambda *a, **k: seen.append((a, k)))
    getattr(localfs, call)(*args, **kwargs)
    assert seen == [expected]


# Nothing else needs a POSIX-only call -----------------------------------------


# The names Windows' os module lacks, and the modules Windows' Python lacks.
POSIX_ONLY_NAMES = {
    "O_NOFOLLOW",
    "O_DIRECTORY",
    "O_CLOEXEC",
    "O_NONBLOCK",
    "O_NOCTTY",
    "O_PATH",
    "getuid",
    "geteuid",
    "getgid",
    "fchmod",
    "fchown",
    "chown",
    "lchown",
    "fork",
    "killpg",
    "setsid",
    "mkfifo",
}
POSIX_ONLY_MODULES = {"fcntl", "resource", "pwd", "grp", "termios", "tty", "pty", "syslog"}
# Each behind its own guard: os.name, hasattr, or an AttributeError it handles.
GUARDED = {
    "service_entry.py": {"getuid"},
    "installer/probe.py": {"getuid"},
    "installer/cli.py": {"geteuid"},
    "installer/execute.py": {"O_NOCTTY"},
}


def modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(PACKAGE).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_only_the_seam_calls_what_windows_lacks():
    for name, tree in modules():
        if name == "localfs.py":
            continue
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in POSIX_ONLY_NAMES
        }
        assert used <= GUARDED.get(name, set()), name


def test_only_the_seam_imports_what_windows_lacks():
    for name, tree in modules():
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.add(node.module.partition(".")[0])
        if name == "localfs.py":
            assert imported & POSIX_ONLY_MODULES == {"fcntl", "pty"}
        else:
            assert not imported & POSIX_ONLY_MODULES, name


def test_every_text_file_names_its_encoding():
    # Windows reads text as cp1252 by default, which once mangled a protected keyword.
    for name, tree in modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            # A positional argument is importlib.metadata's read_text(filename), always UTF-8.
            if node.func.attr in {"read_text", "write_text"} and len(node.args) < (
                1 if node.func.attr == "read_text" else 2
            ):
                keywords = {keyword.arg for keyword in node.keywords}
                assert "encoding" in keywords, f"{name}:{node.lineno}"


def test_every_module_loads_without_the_modules_windows_lacks():
    blocked = sorted(POSIX_ONLY_MODULES)
    code = (
        "import importlib, pkgutil, sys\n"
        # The MCP server's dependencies keep their own guards; only our code is under test.
        f"for name in {blocked!r}:\n"
        "    sys.modules[name] = None\n"
        "import proton_mail_mcp\n"
        "for module in pkgutil.walk_packages(proton_mail_mcp.__path__, 'proton_mail_mcp.'):\n"
        "    if not module.name.endswith('__main__'):\n"
        "        importlib.import_module(module.name)\n"
    )
    result = REAL_RUN(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
