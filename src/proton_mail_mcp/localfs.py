"""The local file calls that differ on Windows, in one place.

On Linux and macOS each function here is the os or fcntl call it is named after, with the
same arguments, and each check is the one the callers made themselves. Nothing changes there.

Windows has no dir_fd, O_NOFOLLOW, flock, file owner or mode bits, so there:

- open goes through CreateFileW. O_NOFOLLOW opens a link itself and refuses it, a directory
  stays open without FILE_SHARE_DELETE, so it cannot be renamed or removed while its descriptor
  lasts, and dir_fd names a file by that directory's current real path. Files can be removed
  while open, as on POSIX, and a descriptor never translates newlines. A rename over an open
  file fails there, so replace waits out a brief holder.
- lock is LockFileEx on one byte far past any file's end, so it never blocks reads or writes.
- Stat has no owner or mode bits to check, so owned, mode_private and world_writable pass there,
  and each caller also asks acl_refusal of the open file or directory. That reads its real owner
  and access rules with GetSecurityInfo: by handle, so the path cannot change under the check, and
  not through icacls, whose output is localized and costs a process per file. A file moved in
  keeps its own rules and a child's rules can differ from its parent's, so a private profile
  proves nothing about what lies inside it. Anything other than this user, SYSTEM and
  Administrators with access refuses, and nothing repairs it: those rules may be the user's choice.
  A new file takes its rules from a folder already checked, and is checked when next opened.
- path_refusal does the same from a path's own object up to the profile folder that holds it, before
  anything exists to open. The environment only says where to stop: a root it names is checked too.

On POSIX acl_refusal, parent_refusal, name_refusal and path_refusal return None and call nothing.
"""

import errno
import functools
import os
import re
import stat
import struct
import sys
import time
from pathlib import Path, PureWindowsPath

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

# O_CLOEXEC and O_NONBLOCK have nothing to add on Windows: its descriptors are never inherited,
# and no FIFO can appear at a file's path. The other two are spare bits only open below reads.
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x10000)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0x20000)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 1
_FILE_SHARE_WRITE = 2
_FILE_SHARE_DELETE = 4
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_LOCKFILE_FAIL_IMMEDIATELY = 1
_LOCKFILE_EXCLUSIVE_LOCK = 2
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_IO_PENDING = 997
# Windows locks are mandatory, so the lock byte lies beyond any data a file can hold.
_LOCK_OFFSET_HIGH = 0x40000000
# A reader or a virus scanner holding a file refuses its rename or removal for a moment.
_BUSY_TRIES = 20
_BUSY_PAUSE = 0.05
_RESERVED_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{digit}" for port in ("COM", "LPT") for digit in "123456789\xb9\xb2\xb3"}
)
# A stream (a:b), a wildcard, a control character, or a short alias, which could name a hidden file.
_RESERVED_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]|~\d')
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 1
_DACL_SECURITY_INFORMATION = 4
_TOKEN_QUERY = 8
_TOKEN_USER = 1
_TOKEN_USER_BYTES = 256
_ACCESS_ALLOWED_ACE = 0
_ACCESS_DENIED_ACE = 1
_MAX_SUB_AUTHORITIES = 15
# What a sandboxed app holds in its token, not an account. A real profile folder carries one.
_CAPABILITY_PREFIX = "S-1-15-3-"
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
# Each stands for the object's owner, which is checked itself. Python's 0700 folders use the first.
_OWNER_ALIASES = frozenset({"S-1-3-4", "S-1-3-0"})
_SID_NAMES = {
    "S-1-1-0": "Everyone",
    "S-1-5-7": "Anonymous",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
}


@functools.cache
def _kernel32():
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(OVERLAPPED),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return kernel32, OVERLAPPED


@functools.cache
def _advapi32():
    import ctypes
    from ctypes import wintypes

    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, pointer]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    return advapi32


def _at(path, dir_fd):
    """A name in an open directory as a full path, which holds while that directory is open."""
    if dir_fd is None:
        return os.fspath(path)
    import ctypes
    import msvcrt

    kernel32, _ = _kernel32()
    handle = msvcrt.get_osfhandle(dir_fd)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            return buffer.value if path == "." else os.path.join(buffer.value, path)
        size = length + 1


def open(path, flags, mode=0o777, **at):
    """os.open; on Windows the file's access rules come from its folder, not from mode."""
    if sys.platform != "win32":
        return os.open(path, flags, mode, **at)
    import ctypes
    import msvcrt

    kernel32, _ = _kernel32()
    access = (_GENERIC_READ, _GENERIC_WRITE, _GENERIC_READ | _GENERIC_WRITE)[flags & 3]
    share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
    attributes = _FILE_ATTRIBUTE_NORMAL
    if flags & O_DIRECTORY:
        attributes |= _FILE_FLAG_BACKUP_SEMANTICS
    else:
        share |= _FILE_SHARE_DELETE
    if flags & O_NOFOLLOW:
        attributes |= _FILE_FLAG_OPEN_REPARSE_POINT
    disposition = _OPEN_EXISTING
    if flags & os.O_CREAT:
        disposition = _CREATE_NEW if flags & os.O_EXCL else _OPEN_ALWAYS
    handle = kernel32.CreateFileW(
        _at(path, at.get("dir_fd")), access, share, None, disposition, attributes, None
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        error = ctypes.get_last_error()
        # Another program holds it for now (a backup, say): busy, not a lasting refusal.
        if error == _ERROR_SHARING_VIOLATION:
            raise BlockingIOError(errno.EWOULDBLOCK, "File is in use")
        raise ctypes.WinError(error)
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, getattr(os, "O_BINARY", 0) | (flags & os.O_APPEND)
        )
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    try:
        info = os.fstat(descriptor)
        if flags & O_NOFOLLOW and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "Refusing a symbolic link or junction")
        if flags & O_DIRECTORY and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def lock(descriptor: int, *, exclusive: bool = True) -> None:
    """flock without waiting: BlockingIOError if held. Closing the descriptor releases it."""
    if sys.platform != "win32":
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return
    import ctypes
    import msvcrt

    kernel32, overlapped = _kernel32()
    flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    region = overlapped(OffsetHigh=_LOCK_OFFSET_HIGH)
    if not kernel32.LockFileEx(
        msvcrt.get_osfhandle(descriptor), flags, 0, 1, 0, ctypes.byref(region)
    ):
        error = ctypes.get_last_error()
        if error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise BlockingIOError(errno.EWOULDBLOCK, "File is locked")
        raise ctypes.WinError(error)


def _when_free(call, *arguments):
    for _ in range(_BUSY_TRIES - 1):
        try:
            return call(*arguments)
        except PermissionError:
            time.sleep(_BUSY_PAUSE)
    return call(*arguments)


def stat_at(path, **at):
    """os.stat for a name, which on Windows may also be relative to an open directory."""
    if sys.platform != "win32":
        return os.stat(path, **at)
    return os.stat(_at(path, at.pop("dir_fd", None)), **at)


def unlink(path, **at):
    if sys.platform != "win32":
        return os.unlink(path, **at)
    return _when_free(os.unlink, _at(path, at.get("dir_fd")))


def replace(source, target, **at):
    if sys.platform != "win32":
        return os.replace(source, target, **at)
    return _when_free(
        os.replace, _at(source, at.get("src_dir_fd")), _at(target, at.get("dst_dir_fd"))
    )


def mkdir(path, mode=0o777, **at):
    """os.mkdir; on Windows Python gives a new directory private access rules for mode 0o700."""
    if sys.platform != "win32":
        return os.mkdir(path, mode, **at)
    return os.mkdir(_at(path, at.get("dir_fd")), mode)


def fsync_directory(descriptor: int) -> None:
    """Make a rename in this directory durable; Windows has no such call, NTFS logs renames."""
    if sys.platform != "win32":
        os.fsync(descriptor)


def fchmod(descriptor: int, mode: int) -> None:
    """Owner-only file modes; Windows has none to set, and acl_refusal checks its rules instead."""
    if sys.platform != "win32":
        os.fchmod(descriptor, mode)


def owned(info: os.stat_result) -> bool:
    """The file is this user's. Windows stat reports no owner: acl_refusal checks that there."""
    return sys.platform == "win32" or info.st_uid == os.getuid()


def mode_private(info: os.stat_result) -> bool:
    """No group or other mode bit is set. Windows has none: acl_refusal checks that there."""
    return sys.platform == "win32" or not info.st_mode & 0o077


def world_writable(info: os.stat_result) -> bool:
    """Everyone may write here. Windows has no such bit: acl_refusal checks that there."""
    return sys.platform != "win32" and bool(info.st_mode & stat.S_IWOTH)


def _sid_text(raw: bytes) -> str:
    """A binary SID as S-1-5-..., from bytes that hold at least the whole SID."""
    if len(raw) < 8:
        raise ValueError("not a SID")
    revision, count = raw[0], raw[1]
    if count > _MAX_SUB_AUTHORITIES or len(raw) < 8 + 4 * count:
        raise ValueError("not a SID")
    authority = int.from_bytes(raw[2:8], "big")
    return "-".join(
        ["S", str(revision), str(authority), *map(str, struct.unpack_from(f"<{count}I", raw, 8))]
    )


def _sid_at(address: int) -> str:
    import ctypes

    count = ctypes.string_at(address, 2)[1]
    if count > _MAX_SUB_AUTHORITIES:
        raise ValueError("not a SID")
    return _sid_text(ctypes.string_at(address, 8 + 4 * count))


@functools.cache
def _my_sid() -> str:
    """The user this process runs as, from its token, not from anything the environment says."""
    import ctypes
    from ctypes import wintypes

    kernel32, _ = _kernel32()
    advapi32 = _advapi32()
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # TOKEN_USER opens with a pointer to its SID, which lies inside this same buffer.
        buffer = ctypes.create_string_buffer(_TOKEN_USER_BYTES)
        needed = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, _TOKEN_USER_BYTES, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_at(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        kernel32.CloseHandle(token)


def _dacl_problem(dacl: bytes, mine: str, *, capabilities: bool = False) -> str | None:
    """What is wrong with these access rules, read as the ACL and ACE layouts Windows documents.

    Access-denied rules take nothing away from what is refused here, so they are passed over.
    An inherited rule counts as any other, and so does an inherit-only one: it gives a folder's
    next file that access. An allow rule of any other kind cannot be read, and refuses.
    capabilities lets rules for app capabilities pass, for path_refusal's last step alone.
    """
    size, count = struct.unpack_from("<HH", dacl, 2)
    offset = 8
    for _ in range(count):
        kind, _flags, length = struct.unpack_from("<BBH", dacl, offset)
        if length < 8 or offset + length > min(size, len(dacl)):
            raise ValueError("not an ACL")
        if kind == _ACCESS_ALLOWED_ACE:
            mask = struct.unpack_from("<I", dacl, offset + 4)[0]
            who = _sid_text(dacl[offset + 8 : offset + length])
            allowed = who in {mine, _SYSTEM, _ADMINISTRATORS} or who in _OWNER_ALIASES
            allowed = allowed or (capabilities and who.startswith(_CAPABILITY_PREFIX))
            if mask and not allowed:
                return f"gives access to {_SID_NAMES.get(who, 'another account')} ({who})"
        elif kind != _ACCESS_DENIED_ACE:
            return "has an access rule of a kind that cannot be checked"
        offset += length
    return None


def _acl_problem(descriptor: int, *, capabilities: bool = False) -> str | None:
    """What keeps this open file or directory from being private; unreadable rules count."""
    import ctypes
    import msvcrt

    kernel32, _ = _kernel32()
    owner, dacl, descriptor_memory = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        mine = _my_sid()
        error = _advapi32().GetSecurityInfo(
            msvcrt.get_osfhandle(descriptor),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor_memory),
        )
        if error:
            return "has access rules that cannot be read"
        try:
            # Success with none: a null DACL, as on FAT, which lets everyone in.
            if not dacl.value:
                return "has no access rules, which gives everyone access"
            if not owner.value:
                return "has no owner that can be read"
            who = _sid_at(owner.value)
            if who not in {mine, _SYSTEM, _ADMINISTRATORS}:
                return f"is owned by another account ({who})"
            size = struct.unpack("<H", ctypes.string_at(dacl.value + 2, 2))[0]
            return _dacl_problem(
                ctypes.string_at(dacl.value, size), mine, capabilities=capabilities
            )
        finally:
            kernel32.LocalFree(descriptor_memory)
    except (OSError, ValueError, struct.error):
        return "has access rules that cannot be read"


# A scanner's brief hold says nothing about the rules, so it is not reported as unreadable ones.
_HELD = "is held open by another program, so its access rules are not known yet; try again"


def _refusal(problem: str | None, what: str = "it") -> str | None:
    if problem is None:
        return None
    return f"must be private on Windows, but {what} {problem} (see docs/windows.md)"


def acl_refusal(descriptor: int) -> str | None:
    """Why this open file or directory is not private on Windows, as text after its name.

    None if only this user, SYSTEM and Administrators have access. Never anything on POSIX, where
    the mode and owner checks beside each call say this, and where this calls nothing.
    """
    if sys.platform != "win32":
        return None
    return _refusal(_acl_problem(descriptor))


def name_refusal(name: str, *, dir_fd: int) -> str | None:
    """acl_refusal for a name in an open directory, which it opens and does not follow."""
    if sys.platform != "win32":
        return None
    descriptor = open(name, os.O_RDONLY | O_NOFOLLOW, dir_fd=dir_fd)
    try:
        return acl_refusal(descriptor)
    finally:
        os.close(descriptor)


def parent_refusal(path) -> str | None:
    """acl_refusal for the folder a path lies in, whose rules a new file there takes on.

    None when that folder does not exist: whoever goes on to create the file finds out.
    """
    if sys.platform != "win32":
        return None
    try:
        descriptor = open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | O_DIRECTORY)
    except FileNotFoundError:
        return None
    except BlockingIOError:
        return _refusal(_HELD, "its folder")
    except OSError:
        return _refusal("has access rules that cannot be read", "its folder")
    try:
        return _refusal(_acl_problem(descriptor), "its folder")
    finally:
        os.close(descriptor)


def state_home() -> Path:
    """This server's folder for its journal and indexes, as the installer's plan gives it."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        root = Path(local) if os.path.isabs(local) else Path.home() / "AppData" / "Local"
        return root / "proton-mail-mcp" / "state"
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path.home() / ".local" / "state"
    return root / "proton-mail-mcp"


def profile_root(path) -> str | None:
    """The outermost of this user's profile folders that holds a path, links resolved, or None."""
    roots = [os.path.expanduser("~"), os.environ.get("LOCALAPPDATA", "")]
    try:
        real = PureWindowsPath(os.path.realpath(path))
        for root in roots:
            if os.path.isabs(root) and real.is_relative_to(os.path.realpath(root)):
                return os.path.realpath(root)
    except (OSError, ValueError):
        pass
    return None


def _open_either(path) -> int:
    """A file or a directory, following links: their real target is what is being checked."""
    try:
        return open(path, os.O_RDONLY | O_DIRECTORY)
    except NotADirectoryError:
        return open(path, os.O_RDONLY)


def path_refusal(path) -> str | None:
    """Why a path cannot hold private data on Windows, as text after its name; None on POSIX.

    It must lie in this user's profile, and it and each folder from it up to the profile folder,
    as far as they exist, must be private. Links resolved, as the file calls later see them.
    The profile folder itself may also carry rules for app capabilities: Windows Server 2025 gave
    a real one such a rule, which its AppData did not inherit. Anything below is held to all of it.
    """
    if sys.platform != "win32":
        return None
    root = profile_root(path)
    if root is None:
        return "must be inside your user profile on Windows"
    step = os.path.realpath(path)
    while True:
        top = PureWindowsPath(step) == PureWindowsPath(root)
        try:
            descriptor = _open_either(step)
        except FileNotFoundError:
            pass
        except BlockingIOError:
            return _refusal(_HELD, step)
        except OSError:
            return _refusal("has access rules that cannot be read", step)
        else:
            try:
                problem = _acl_problem(descriptor, capabilities=top)
            finally:
                os.close(descriptor)
            if problem:
                return _refusal(problem, step)
        if top:
            return None
        parent = os.path.dirname(step)
        if parent == step:  # A link moved after profile_root looked: the root is not above this.
            return "must be inside your user profile on Windows"
        step = parent


def reserved_name(name: str) -> bool:
    """Whether Windows would take this as anything but one plain file's name. Never on POSIX."""
    return sys.platform == "win32" and (
        name[-1:] in {".", " "}
        or _RESERVED_CHARS.search(name) is not None
        or name.partition(".")[0].rstrip(" ").upper() in _RESERVED_DEVICES
    )


def plain_name(name: str) -> str:
    """A sender's file name, changed only where reserved_name refuses it."""
    if not reserved_name(name):
        return name
    plain = _RESERVED_CHARS.sub("_", name).rstrip(" .") or "_"
    return f"_{plain}" if reserved_name(plain) else plain
