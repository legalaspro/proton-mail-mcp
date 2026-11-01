"""Installer OS detection from os-release text and platform facts."""

import pytest

from proton_mail_mcp.installer.detect import (
    FAMILIES,
    detect,
    linux_family,
    normalize_machine,
    parse_os_release,
    simulated,
)

UBUNTU = """\
PRETTY_NAME="Ubuntu 24.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
ID=ubuntu
ID_LIKE=debian
"""


def release(os_id, like=None, version="1"):
    text = f"ID={os_id}\nVERSION_ID={version}\nPRETTY_NAME='{os_id} {version}'\n"
    return text + (f'ID_LIKE="{like}"\n' if like else "")


def test_os_release_parsing_follows_shell_quoting():
    values = parse_os_release(
        "# comment\n"
        'NAME="Fedora Linux"\n'
        "ID=fedora\n"
        "VERSION_ID=42\n"
        "PRETTY_NAME='Fedora Linux 42 (Workstation)'\n"
        'HOME_URL="https://example.test/\\"q\\""\n'
        "BROKEN=unterminated 'quote\n"
        "not an assignment\n"
    )
    assert values["NAME"] == "Fedora Linux"
    assert values["ID"] == "fedora"
    assert values["PRETTY_NAME"] == "Fedora Linux 42 (Workstation)"
    assert values["HOME_URL"] == 'https://example.test/"q"'
    assert "BROKEN" not in values


@pytest.mark.parametrize(
    ("os_id", "like", "family"),
    [
        ("ubuntu", "debian", "debian"),
        ("debian", None, "debian"),
        ("linuxmint", "ubuntu debian", "debian"),
        ("pop", "ubuntu debian", "debian"),
        ("neon", "ubuntu debian", "debian"),
        ("fedora", None, "fedora"),
        ("rhel", "fedora", "fedora"),
        ("almalinux", "rhel centos fedora", "fedora"),
        ("rocky", "rhel centos fedora", "fedora"),
        ("centos", "rhel fedora", "fedora"),
        ("ol", "fedora", "fedora"),
        ("amzn", "centos rhel fedora", "fedora"),
        ("arch", None, "arch"),
        ("manjaro", "arch", "arch"),
        ("endeavouros", "arch", "arch"),
        ("someforkos", "arch", "arch"),
        ("opensuse-tumbleweed", "opensuse suse", "unsupported"),
        ("alpine", None, "unsupported"),
        ("gentoo", None, "unsupported"),
        ("nixos", None, "unsupported"),
    ],
)
def test_linux_families(os_id, like, family):
    assert linux_family(parse_os_release(release(os_id, like))) == family
    platform = detect("Linux", "x86_64", release(os_id, like))
    assert platform.family == family
    assert platform.supported is (family != "unsupported")
    assert platform.system == "linux"


def test_ubuntu_details():
    platform = detect("Linux", "x86_64", UBUNTU)
    assert (platform.family, platform.os_id, platform.version) == ("debian", "ubuntu", "24.04")
    assert platform.name == "Ubuntu 24.04.1 LTS"


def test_missing_os_release_is_unsupported_linux():
    platform = detect("Linux", "x86_64", None)
    assert platform.family == "unsupported" and platform.system == "linux"


@pytest.mark.parametrize(
    ("system", "family"),
    [("Darwin", "macos"), ("Windows", "windows"), ("FreeBSD", "unsupported"), ("", "unsupported")],
)
def test_non_linux_systems(system, family):
    platform = detect(system, "arm64", None, "15.1")
    assert platform.family == family
    assert platform.supported is (family != "unsupported")
    if family == "unsupported":
        assert platform.refusal.startswith("Unsupported platform")


def test_windows_is_detected_and_supported():
    platform = detect("Windows", "AMD64", None, "10.0.26100")
    assert (platform.family, platform.system, platform.machine) == ("windows", "windows", "x86_64")
    assert platform.supported
    assert platform.refusal is None


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("x64", "x86_64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
        ("riscv64", "riscv64"),
        ("", "unknown"),
    ],
)
def test_machine_names_are_normalized(machine, expected):
    assert normalize_machine(machine) == expected


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_can_be_simulated(family):
    platform = simulated(family)
    assert platform.family == family
    assert platform.supported
    assert "simulated" in platform.name
