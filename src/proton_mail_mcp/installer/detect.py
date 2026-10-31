"""Operating system detection from os-release text and platform facts."""

import platform as _platform
import shlex
from dataclasses import dataclass
from pathlib import Path

FAMILIES = ("debian", "fedora", "arch", "macos", "windows")
# Detected and simulated, but refused: plans for these stay buildable for tests only.
NOT_YET_SUPPORTED = {
    "windows": (
        "Windows is not yet supported: the MCP server keeps its audit, undo and index "
        "state with POSIX file locking (fcntl), so its service cannot start on Windows. "
        "Use WSL2 (run `wsl --install`, then run this command inside Linux); native "
        "Windows support is planned for v1.1. See docs/install.md."
    ),
}
# platform.system() of Python builds that run on Windows without being a Linux: the
# prefixes install.sh refuses as msys, cygwin and win32 before Python even starts.
_WINDOWS_LAYERS = ("cygwin", "msys", "mingw")
# Families sharing a package format; ID_LIKE covers derivatives not listed here.
_LINUX_IDS = {
    "debian": {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "raspbian", "kali"},
    "fedora": {"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "nobara"},
    "arch": {"arch", "archarm", "manjaro", "endeavouros", "garuda", "artix", "cachyos"},
}
_LIKE = {
    "debian": "debian",
    "ubuntu": "debian",
    "fedora": "fedora",
    "rhel": "fedora",
    "centos": "fedora",
    "arch": "arch",
}
_MACHINES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
OS_RELEASE_PATHS = ("/etc/os-release", "/usr/lib/os-release")


@dataclass(frozen=True)
class Platform:
    family: str  # debian | fedora | arch | macos | windows | unsupported
    os_id: str
    version: str
    machine: str
    name: str

    @property
    def system(self) -> str:
        if self.family in {"macos", "windows"}:
            return self.family
        return "linux"

    @property
    def refusal(self) -> str | None:
        """Why the installer refuses this platform, or None when it is supported."""
        if self.family in NOT_YET_SUPPORTED:
            return NOT_YET_SUPPORTED[self.family]
        if self.family not in FAMILIES:
            return f"Unsupported platform: {self.name}"
        return None

    @property
    def supported(self) -> bool:
        return self.refusal is None


def parse_os_release(text: str) -> dict[str, str]:
    """Parse the shell-compatible KEY=value assignments of os-release(5)."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if not key.isidentifier():
            continue
        try:
            words = shlex.split(raw, posix=True)
        except ValueError:
            continue
        values[key] = words[0] if len(words) == 1 else ""
    return values


def linux_family(release: dict[str, str]) -> str:
    os_id = release.get("ID", "").lower()
    for family, ids in _LINUX_IDS.items():
        if os_id in ids:
            return family
    for like in release.get("ID_LIKE", "").lower().split():
        if like in _LIKE:
            return _LIKE[like]
    return "unsupported"


def normalize_machine(machine: str) -> str:
    return _MACHINES.get(machine.strip().lower(), machine.strip().lower() or "unknown")


def detect(
    system: str,
    machine: str,
    os_release: str | None = None,
    release: str = "",
) -> Platform:
    """Classify a host from platform.system(), platform.machine() and os-release."""
    arch = normalize_machine(machine)
    kind = system.strip().lower()
    if kind == "darwin":
        return Platform("macos", "macos", release, arch, f"macOS {release}".strip())
    if kind == "windows" or kind.startswith(_WINDOWS_LAYERS):
        return Platform("windows", "windows", release, arch, f"Windows {release}".strip())
    if kind == "linux":
        values = parse_os_release(os_release or "")
        return Platform(
            linux_family(values),
            values.get("ID", "linux").lower() or "linux",
            values.get("VERSION_ID", ""),
            arch,
            values.get("PRETTY_NAME") or values.get("NAME") or "Linux",
        )
    return Platform("unsupported", kind or "unknown", release, arch, system or "unknown")


def simulated(family: str) -> Platform:
    """A representative platform for --simulate dry runs on any host."""
    names = {
        "debian": ("ubuntu", "24.04", "Ubuntu 24.04 LTS (simulated)"),
        "fedora": ("fedora", "42", "Fedora Linux 42 (simulated)"),
        "arch": ("arch", "", "Arch Linux (simulated)"),
        "macos": ("macos", "15.0", "macOS 15.0 (simulated)"),
        "windows": ("windows", "10.0.26100", "Windows 11 (simulated)"),
    }
    os_id, version, name = names[family]
    return Platform(family, os_id, version, "x86_64" if family != "macos" else "arm64", name)


def detect_host() -> Platform:
    system = _platform.system()
    if system == "Darwin":
        release = _platform.mac_ver()[0]
    elif system == "Windows":
        release = _platform.version()
    else:
        release = ""
    text = None
    if system == "Linux":
        for candidate in OS_RELEASE_PATHS:
            try:
                text = Path(candidate).read_text(encoding="utf-8", errors="replace")
                break
            except OSError:
                continue
    return detect(system, _platform.machine(), text, release)
