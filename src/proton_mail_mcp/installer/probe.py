"""Read-only host probes, plus a simulated host for dry runs and tests."""

import ntpath
import os
import posixpath
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .. import credential_store
from . import templates
from .detect import Platform
from .plan import MANAGER_ENVIRONMENT, SYSTEMD_BOOTED

_PROBE_TIMEOUT = 20
# A size walk stops here rather than run for minutes; the size is then unknown.
MAX_SIZE_ENTRIES = 200_000


class HostProbe:
    """Facts about the real host; every call is read-only."""

    def __init__(self, platform: Platform) -> None:
        self.env = dict(os.environ)
        self.home = str(Path.home())
        if platform.family == "windows":
            domain = self.env.get("USERDOMAIN", "")
            user = self.env.get("USERNAME", "")
            self.user_id = f"{domain}\\{user}" if domain else user
        else:
            self.user_id = str(os.getuid()) if hasattr(os, "getuid") else ""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def realpath(self, path: str) -> str:
        """The path with symlinks and ".." resolved, as far as it exists."""
        try:
            return os.path.realpath(path)
        except (OSError, ValueError):
            return os.path.normpath(path)

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def readlink(self, path: str) -> str | None:
        """A symbolic link's target as written; None for anything else."""
        try:
            return os.readlink(path) if os.path.islink(path) else None
        except (OSError, ValueError):
            return None

    def tree_size(self, path: str) -> int | None:
        """Bytes of the regular files at or below path; links are never followed.

        None when path is a symbolic link, part of it cannot be read, or it holds more
        than MAX_SIZE_ENTRIES entries.
        """
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                return None
            if not stat.S_ISDIR(info.st_mode):
                return info.st_size if stat.S_ISREG(info.st_mode) else 0
            total, seen, pending = 0, 0, [path]
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_SIZE_ENTRIES:
                            return None
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            return None
        return total

    def succeeds(self, argv: tuple[str, ...]) -> bool:
        return self.output(argv) is not None

    def output(self, argv: tuple[str, ...]) -> str | None:
        """Text output of a successful read-only command, or None."""
        try:
            result = subprocess.run(
                list(argv),
                capture_output=True,
                check=False,
                timeout=_PROBE_TIMEOUT,
                text=True,
                errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 else None

    def can_make_venv(self, python: str) -> bool:
        """`python -m venv` needs ensurepip, a separate package on Debian and Ubuntu."""
        return self.succeeds((python, "-c", "import ensurepip"))

    def has_secret(
        self, store: str, service: str, account: str, locations: Mapping[str, str] | None = None
    ) -> bool | None:
        """None when the store could not be checked: unknown, never absent."""
        try:
            return credential_store.exists(store, service, account, locations)
        except credential_store.CredentialStoreError:
            return None


@dataclass
class FakeProbe:
    """A scripted host: nothing exists unless listed."""

    env: dict[str, str] = field(default_factory=dict)
    home: str = "/home/you"
    user_id: str = "1000"
    tools: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    succeeding: set[tuple[str, ...]] = field(default_factory=set)
    outputs: dict[tuple[str, ...], str] = field(default_factory=dict)
    secrets: set[tuple[str, str, str]] = field(default_factory=set)
    # Symbolic links, link path -> target as written (relative to the link's directory
    # unless absolute).
    links: dict[str, str] = field(default_factory=dict)
    # The pass locations each has_secret call used.
    secret_locations: list[dict[str, str]] = field(default_factory=list)
    venv_module: bool = True

    def which(self, name: str) -> str | None:
        return self.tools.get(name)

    def exists(self, path: str) -> bool:
        return path in self.files

    def realpath(self, path: str) -> str:
        """Normalise ".." first, then follow `links`; close enough for planning tests."""
        flavor = ntpath if "\\" in path else posixpath
        resolved = flavor.normpath(path)
        for _hop in range(40):
            for link, target in self.links.items():
                if resolved == link or resolved.startswith(link + flavor.sep):
                    if not flavor.isabs(target):
                        target = flavor.join(flavor.dirname(link), target)
                    resolved = flavor.normpath(target + resolved[len(link) :])
                    break
            else:
                break
        return resolved

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def readlink(self, path: str) -> str | None:
        return self.links.get(path)

    def tree_size(self, path: str) -> int | None:
        """The encoded lengths of the listed files at or below path; links not followed."""
        if path in self.links:
            return None
        sep = "\\" if "\\" in path else "/"
        prefix = path.rstrip(sep) + sep
        return sum(
            len(text.encode("utf-8"))
            for name, text in self.files.items()
            if name == path or name.startswith(prefix)
        )

    def succeeds(self, argv: tuple[str, ...]) -> bool:
        return tuple(argv) in self.succeeding or tuple(argv) in self.outputs

    def output(self, argv: tuple[str, ...]) -> str | None:
        argv = tuple(argv)
        if argv in self.outputs or argv in self.succeeding:
            return self.outputs.get(argv, "")
        return None

    def can_make_venv(self, python: str) -> bool:
        return self.venv_module

    def has_secret(
        self, store: str, service: str, account: str, locations: Mapping[str, str] | None = None
    ) -> bool:
        self.secret_locations.append(dict(locations or {}))
        return (store, service, account) in self.secrets


_LINUX_TOOLS = ("secret-tool", "pass", "gpg", "debsig-verify", "systemctl")


def simulated_probe(platform: Platform) -> FakeProbe:
    """A fresh host of the given family with the usual tools on PATH."""
    if platform.family == "windows":
        return FakeProbe(
            env={
                "LOCALAPPDATA": "C:\\Users\\you\\AppData\\Local",
                "ProgramFiles": "C:\\Program Files",
                "USERDOMAIN": "HOST",
                "USERNAME": "you",
            },
            home="C:\\Users\\you",
            user_id="HOST\\you",
            tools={"winget": "C:\\Users\\you\\AppData\\Local\\Microsoft\\WindowsApps\\winget.exe"},
        )
    if platform.family == "macos":
        return FakeProbe(
            home="/Users/you",
            user_id="501",
            tools={"brew": "/opt/homebrew/bin/brew"},
        )
    # systemd answers an empty FragmentPath for a unit it does not load.
    unloaded = {
        ("systemctl", "--user", "show", "--property=FragmentPath", "--value", unit): "\n"
        for unit in (templates.BRIDGE_UNIT, templates.MCP_UNIT)
    }
    return FakeProbe(
        tools={name: f"/usr/bin/{name}" for name in _LINUX_TOOLS},
        files={SYSTEMD_BOOTED: ""},
        outputs={
            MANAGER_ENVIRONMENT: "HOME=/home/you\nLANG=C.UTF-8\nPATH=/usr/bin:/bin\n",
            **unloaded,
        },
    )
