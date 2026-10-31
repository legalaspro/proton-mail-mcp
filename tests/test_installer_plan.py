"""Installer plans for every platform, built on Linux from simulated hosts."""

import ast
import dataclasses
import io
import os
import plistlib
import shlex
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from proton_mail_mcp import service_entry
from proton_mail_mcp.config import Settings
from proton_mail_mcp.credential_store import BRIDGE_SERVICE, TOKEN_SERVICE
from proton_mail_mcp.installer import plan as planner
from proton_mail_mcp.installer import templates
from proton_mail_mcp.installer.detect import FAMILIES, NOT_YET_SUPPORTED, Platform, simulated
from proton_mail_mcp.installer.execute import Executor
from proton_mail_mcp.installer.plan import (
    BRIDGE_KEY_FINGERPRINT,
    BRIDGE_KEY_ID,
    MACOS_BRIDGE_EXE,
    MANAGER_ENVIRONMENT,
    PREVIOUS_VENVS_KEY,
    VENV_KEY,
    Options,
    PlanError,
    install_plan,
    install_steps,
    paths_for,
    uninstall_plan,
    uninstall_steps,
)
from proton_mail_mcp.installer.probe import FakeProbe, HostProbe, simulated_probe
from proton_mail_mcp.installer.steps import (
    DIR_MARKER,
    MARKER,
    CheckDebSignature,
    CheckKey,
    CheckPolicy,
    CheckRpmSignature,
    CheckStopped,
    Command,
    DeleteSecret,
    Download,
    Note,
    RemovePath,
    StoreSecret,
    WaitForPort,
    WriteFile,
)
from proton_mail_mcp.security import get_credentials
from proton_mail_mcp.service_entry import parse_env_file

USER = "you@example.com"
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SUPPORTED = tuple(family for family in FAMILIES if family not in NOT_YET_SUPPORTED)
# install_plan and uninstall_plan refuse Windows; its builders stay tested directly.
UNGUARDED = {install_plan: install_steps, uninstall_plan: uninstall_steps}
MCP_TASK = "\\proton-mail-mcp-HOST-you"
BRIDGE_TASK = "\\proton-mail-mcp-bridge-HOST-you"


def options(**overrides):
    values = {"bridge_user": USER, "source": "/src/checkout", "python": "/usr/bin/python3"}
    return Options(**{**values, **overrides})


def build(family, probe=None, action=install_plan, platform=None, **overrides):
    platform = platform or simulated(family)
    probe = probe or simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    if platform.family in NOT_YET_SUPPORTED:
        action = UNGUARDED.get(action, action)
    return action(platform, options(**overrides), probe, paths)


def task_query(name):
    return ("schtasks", "/Query", "/TN", name, "/XML")


def unit_fragment(unit):
    return ("systemctl", "--user", "show", "--property=FragmentPath", "--value", unit)


def notes(steps):
    return " ".join(step.text for step in steps if isinstance(step, Note))


def commands(steps):
    return [step.argv for step in steps if isinstance(step, Command)]


def index(steps, predicate):
    return next(number for number, step in enumerate(steps) if predicate(step))


def written(steps, suffix):
    return next(
        step for step in steps if isinstance(step, WriteFile) and step.path.endswith(suffix)
    )


def env_file(steps):
    return parse_env_file(written(steps, "proton-mail-mcp.env").content)


@pytest.mark.parametrize("family", FAMILIES)
def test_every_plan_binds_loopback_keeps_confirmation_and_holds_no_secret(family):
    steps = build(family)
    settings = env_file(steps)
    assert settings["PROTON_MCP_HOST"] == "127.0.0.1"
    assert settings["PROTON_BRIDGE_HOST"] == "127.0.0.1"
    assert settings["PROTON_MCP_CONFIRM_MUTATIONS"] == "1"
    assert settings["PROTON_BRIDGE_USER"] == USER
    assert "PROTON_MCP_ENABLE_SEND" not in settings
    for step in steps:
        text = repr(step)
        assert "PROTON_MCP_TOKEN" not in text and "PROTON_BRIDGE_PASSWORD" not in text
        if isinstance(step, WriteFile):
            assert MARKER in step.content
    secrets = [step for step in steps if isinstance(step, StoreSecret)]
    assert [(step.service, step.source) for step in secrets] == [
        (BRIDGE_SERVICE, "prompt"),
        (TOKEN_SERVICE, "generate"),
    ]


@pytest.mark.parametrize("family", FAMILIES)
def test_secrets_and_settings_are_written_before_services_start(family):
    steps = build(family)
    last_secret = max(i for i, step in enumerate(steps) if isinstance(step, StoreSecret))
    settings = index(steps, lambda s: isinstance(s, WriteFile) and s.path.endswith(".env"))
    first_start = index(
        steps,
        lambda s: (
            isinstance(s, Command)
            and any(word in s.argv for word in ("start", "bootstrap", "/Run"))
        ),
    )
    assert last_secret < settings < first_start


def test_debian_verifies_proton_signature_before_apt_installs():
    steps = build("debian")
    downloads = [step.url for step in steps if isinstance(step, Download)]
    assert downloads == [
        "https://proton.me/download/bridge/bridge_pubkey.gpg",
        "https://proton.me/download/bridge/protonmail-bridge_3.27.1-1_amd64.deb",
        "https://proton.me/download/bridge/bridge.pol",
    ]
    key = index(steps, lambda s: isinstance(s, CheckKey))
    policy = index(steps, lambda s: isinstance(s, CheckPolicy))
    verify = index(steps, lambda s: isinstance(s, CheckDebSignature))
    install = index(
        steps,
        lambda s: (
            isinstance(s, Command)
            and s.argv[:3] == ("apt-get", "install", "-y")
            and s.argv[3].endswith(".deb")
        ),
    )
    assert key < policy < verify < install
    assert steps[key].fingerprint == BRIDGE_KEY_FINGERPRINT
    assert steps[policy].key_id == BRIDGE_KEY_ID == "E2C75D68E6234B07"
    cache = "/home/you/.cache/proton-mail-mcp/installer"
    package = f"{cache}/protonmail-bridge_3.27.1-1_amd64.deb"
    assert steps[verify] == CheckDebSignature(
        package,
        f"{cache}/bridge_pubkey.gpg",
        f"{cache}/bridge.pol",
        BRIDGE_KEY_ID,
        BRIDGE_KEY_FINGERPRINT,
        steps[verify].summary,
    )
    assert steps[install].sudo and steps[install].argv[-1] == package
    # The system debsig directories, which trust every configured origin, are never
    # used or changed; the only privileged commands are apt's.
    assert not any(argv[0] in {"debsig-verify", "gpg", "install"} for argv in commands(steps))
    assert not any("/debsig/" in repr(step) for step in steps)
    assert {step.argv[0] for step in steps if isinstance(step, Command) and step.sudo} == {
        "apt-get"
    }


def test_fedora_checks_the_rpm_against_proton_key_alone_before_dnf():
    steps = build("fedora")
    key = index(steps, lambda s: isinstance(s, CheckKey))
    checked = index(steps, lambda s: isinstance(s, CheckRpmSignature))
    installed = index(steps, lambda s: isinstance(s, Command) and s.argv[:2] == ("dnf", "install"))
    assert key < checked < installed
    cache = "/home/you/.cache/proton-mail-mcp/installer"
    package = f"{cache}/protonmail-bridge-3.27.1-1.x86_64.rpm"
    assert steps[key].fingerprint == BRIDGE_KEY_FINGERPRINT
    assert (steps[checked].package, steps[checked].key) == (package, f"{cache}/bridge_pubkey.gpg")
    assert steps[installed].argv[-1] == package
    # The system rpm database, which trusts every repository key, is never used or changed.
    assert not any(argv[0] == "rpm" for argv in commands(steps))


def test_the_pinned_fingerprint_ends_in_the_published_key_id():
    assert len(BRIDGE_KEY_FINGERPRINT) == 40
    assert BRIDGE_KEY_FINGERPRINT == BRIDGE_KEY_FINGERPRINT.upper()
    assert BRIDGE_KEY_FINGERPRINT.endswith("E2C75D68E6234B07")


def test_arch_uses_the_signed_repository_package():
    steps = build("arch")
    assert ("pacman", "-S", "--needed", "--noconfirm", "protonmail-bridge-core") in commands(steps)
    assert not any(isinstance(step, Download) for step in steps)
    unit = written(steps, templates.BRIDGE_UNIT).content
    assert "ExecStart=/usr/bin/protonmail-bridge-core --noninteractive" in unit


def test_bridge_version_can_be_pinned_and_is_validated():
    steps = build("fedora", bridge_version="3.30.0")
    assert any(
        "protonmail-bridge-3.30.0-1" in step.url for step in steps if isinstance(step, Download)
    )
    with pytest.raises(PlanError, match="bridge-version"):
        build("fedora", bridge_version="3.30.0; rm -rf /")


@pytest.mark.parametrize("family", ["debian", "fedora"])
def test_linux_packages_are_x86_64_only(family):
    platform = replace(simulated(family), machine="arm64")
    with pytest.raises(PlanError, match="x86_64 only"):
        build(family, platform=platform)
    assert build(family, platform=platform, skip_bridge=True, skip_bridge_service=True)


def test_arm_arch_uses_its_repository():
    platform = replace(simulated("arch"), machine="arm64")
    assert ("pacman", "-S", "--needed", "--noconfirm", "protonmail-bridge-core") in commands(
        build("arch", platform=platform)
    )


def test_macos_installs_the_cask_and_checks_proton_signature():
    steps = build("macos")
    argv = commands(steps)
    assert ("/opt/homebrew/bin/brew", "install", "--cask", "proton-mail-bridge") in argv
    assert (
        "codesign",
        "--verify",
        "--deep",
        "--strict",
        "/Applications/Proton Mail Bridge.app",
    ) in argv
    assert (
        "spctl",
        "--assess",
        "--type",
        "execute",
        "/Applications/Proton Mail Bridge.app",
    ) in argv
    identity = next(step for step in steps if isinstance(step, Command) and "-dv" in step.argv)
    assert identity.expect == "Authority=Developer ID Application: Proton"


def test_macos_without_homebrew_explains_the_alternatives():
    probe = simulated_probe(simulated("macos"))
    probe.tools.clear()
    with pytest.raises(PlanError, match="--skip-bridge"):
        build("macos", probe=probe)


def test_macos_launch_agents_hold_no_secret_and_run_the_service_launcher():
    steps = build("macos")
    mcp = plistlib.loads(written(steps, "com.proton-mail-mcp.plist").content.encode())
    support = "/Users/you/Library/Application Support/proton-mail-mcp"
    assert mcp["Label"] == "com.proton-mail-mcp"
    assert mcp["ProgramArguments"] == [
        f"{support}/venv/bin/proton-mail-mcp-service",
        "--env-file",
        f"{support}/proton-mail-mcp.env",
    ]
    assert "EnvironmentVariables" not in mcp
    assert mcp["Umask"] == 0o077 and mcp["KeepAlive"] is True
    bridge = plistlib.loads(written(steps, "com.proton-mail-mcp.bridge.plist").content.encode())
    assert bridge["ProgramArguments"] == [MACOS_BRIDGE_EXE, "--noninteractive"]
    assert bridge["KeepAlive"] == {"SuccessfulExit": False}


def test_macos_reload_only_follows_changes_once_loaded():
    platform = simulated("macos")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    plist = f"{paths.agents_dir}/com.proton-mail-mcp.plist"
    probe.outputs[("launchctl", "print", "gui/501/com.proton-mail-mcp")] = (
        f"gui/501/com.proton-mail-mcp = {{\n\tpath = {plist}\n\tstate = running\n}}\n"
    )
    probe.files[plist] = f"<!-- {MARKER} -->"
    steps = build("macos", probe=probe)
    bootstrap = [step for step in steps if isinstance(step, Command) and "bootstrap" in step.argv]
    by_path = {step.argv[-1].rsplit("/", 1)[-1]: step for step in bootstrap}
    assert by_path["com.proton-mail-mcp.plist"].when_changed == ("mcp-agent",)
    assert by_path["com.proton-mail-mcp.bridge.plist"].when_changed == ()
    kickstart = next(
        step for step in steps if isinstance(step, Command) and "kickstart" in step.argv
    )
    assert set(kickstart.when_changed) == {"mcp-package", "env-file", "credentials"}


def test_windows_uses_winget_authenticode_and_logon_tasks():
    steps = build("windows")
    winget = next(step for step in steps if isinstance(step, Command) and step.argv[0] == "winget")
    assert winget.argv[:5] == ("winget", "install", "--id", "Proton.ProtonMailBridge", "--exact")
    authenticode = next(
        step for step in steps if isinstance(step, Command) and step.argv[0] == "powershell"
    )
    exe = "C:\\Program Files\\Proton AG\\Proton Mail Bridge\\proton-bridge.exe"
    assert authenticode.env == (("PMM_VERIFY_PATH", exe),)
    assert '"' not in authenticode.argv[-1] and "Proton( Technologies)? AG" in authenticode.argv[-1]
    task = written(steps, "mcp-task.xml")
    assert task.encoding == "utf-16"
    root = ET.fromstring(task.content.split("\n", 1)[1])
    principal = root.find("t:Principals/t:Principal", TASK_NS)
    assert principal.find("t:LogonType", TASK_NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", TASK_NS).text == "LeastPrivilege"
    assert principal.find("t:UserId", TASK_NS).text == "HOST\\you"
    action = root.find("t:Actions/t:Exec", TASK_NS)
    base = "C:\\Users\\you\\AppData\\Local\\proton-mail-mcp"
    assert action.find("t:Command", TASK_NS).text == f"{base}\\venv\\Scripts\\pythonw.exe"
    assert action.find("t:Arguments", TASK_NS).text == (
        f"-m proton_mail_mcp.service_entry --env-file {base}\\proton-mail-mcp.env "
        f"--log-file {base}\\logs\\mcp.log"
    )
    assert MARKER in root.find("t:RegistrationInfo/t:Description", TASK_NS).text
    bridge = ET.fromstring(written(steps, "bridge-task.xml").content.split("\n", 1)[1])
    bridge_action = bridge.find("t:Actions/t:Exec", TASK_NS)
    assert bridge_action.find("t:Command", TASK_NS).text == exe
    assert bridge_action.find("t:Arguments", TASK_NS).text == "--noninteractive"


def test_windows_stops_a_registered_mcp_task_before_pip_replaces_files():
    platform = simulated("windows")
    probe = simulated_probe(platform)
    probe.outputs[task_query(MCP_TASK)] = f"<Task><Description>{MARKER}.</Description></Task>"
    steps = build("windows", probe=probe)
    stop = index(steps, lambda s: isinstance(s, Command) and s.argv[:2] == ("schtasks", "/End"))
    pip = index(steps, lambda s: isinstance(s, Command) and "pip" in s.argv)
    assert stop < pip
    create = next(
        step
        for step in steps
        if isinstance(step, Command)
        and step.argv[:4] == ("schtasks", "/Create", "/TN", "\\proton-mail-mcp-HOST-you")
    )
    assert create.when_changed == ("mcp-task",)


def test_windows_without_winget_explains_the_alternatives():
    probe = simulated_probe(simulated("windows"))
    probe.tools.clear()
    with pytest.raises(PlanError, match="-SkipBridge"):
        build("windows", probe=probe)


def test_systemd_units_golden():
    bridge = templates.systemd_bridge_unit("/usr/bin/protonmail-bridge")
    mcp = templates.systemd_mcp_unit(
        "/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp-service",
        "/home/you/.config/proton-mail-mcp/proton-mail-mcp.env",
        8765,
        True,
    )
    assert bridge == (
        f"# {MARKER}; rerun the installer instead of editing.\n"
        "[Unit]\n"
        "Description=Proton Mail Bridge (noninteractive) for proton-mail-mcp\n"
        "Documentation=https://proton.me/support/bridge-cli-guide\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/usr/bin/protonmail-bridge --noninteractive\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    assert mcp == (
        f"# {MARKER}; rerun the installer instead of editing.\n"
        "[Unit]\n"
        "Description=proton-mail-mcp MCP server on http://127.0.0.1:8765/mcp\n"
        "After=proton-mail-mcp-bridge.service\n"
        "Wants=proton-mail-mcp-bridge.service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp-service "
        "--env-file /home/you/.config/proton-mail-mcp/proton-mail-mcp.env\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "UMask=0077\n"
        "NoNewPrivileges=true\n"
        "LockPersonality=true\n"
        "RestrictRealtime=true\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    standalone = templates.systemd_mcp_unit("/bin/service", "/etc/env", 9000, False)
    assert "Wants=" not in standalone and "After=" not in standalone


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/usr/bin/plain", "/usr/bin/plain"),
        ("/home/a b/x", '"/home/a b/x"'),
        ("/home/100%/x", "/home/100%%/x"),
        ("/home/$USER/x", '"/home/$$USER/x"'),
        ('/home/q"uote', '"/home/q\\"uote"'),
        ("/home/back\\slash", '"/home/back\\\\slash"'),
    ],
)
def test_systemd_arguments_are_quoted_and_unexpanded(value, expected):
    assert templates.systemd_arg(value) == expected


def test_line_breaks_cannot_reach_a_service_definition():
    with pytest.raises(templates.TemplateError):
        templates.systemd_arg("/bin/x\nExecStartPre=/bin/evil")
    with pytest.raises(PlanError, match="line breaks"):
        build("arch", bridge_bin="/bin/x\nExecStartPre=/bin/evil")


def test_linux_services_restart_only_for_relevant_changes():
    steps = build("fedora")
    restart = {
        step.argv[-1]: step.when_changed
        for step in steps
        if isinstance(step, Command) and step.argv[:3] == ("systemctl", "--user", "restart")
    }
    assert restart[templates.BRIDGE_UNIT] == ("bridge-unit",)
    assert set(restart[templates.MCP_UNIT]) == {
        "mcp-unit",
        "mcp-package",
        "env-file",
        "credentials",
    }
    enable = next(step for step in steps if isinstance(step, Command) and "enable" in step.argv)
    unit_dir = "/home/you/.config/systemd/user"
    assert enable.argv[3:] == (
        f"{unit_dir}/{templates.BRIDGE_UNIT}",
        f"{unit_dir}/{templates.MCP_UNIT}",
    )


@pytest.mark.parametrize("family", ["debian", "fedora", "arch"])
@pytest.mark.parametrize("bridge_service", [True, False])
def test_linux_units_are_enabled_by_path_and_reloaded_before_any_start(family, bridge_service):
    # Sol: units under a shell-only XDG_CONFIG_HOME are outside a running user
    # manager's search path. Enabling them by path makes systemd link them into its
    # own configuration directory; the reload then precedes every start or restart.
    platform = simulated(family)
    probe = simulated_probe(platform)
    probe.env["XDG_CONFIG_HOME"] = "/srv/shell-config"
    steps = build(family, probe=probe, skip_bridge_service=not bridge_service)
    unit_dir = "/srv/shell-config/systemd/user"
    units = ([templates.BRIDGE_UNIT] if bridge_service else []) + [templates.MCP_UNIT]
    unit_files = [f"{unit_dir}/{unit}" for unit in units]
    writes = [
        number
        for number, step in enumerate(steps)
        if isinstance(step, WriteFile) and step.path in unit_files
    ]
    assert [steps[number].path for number in writes] == unit_files
    systemctl = [
        (number, step)
        for number, step in enumerate(steps)
        if isinstance(step, Command) and step.argv[:2] == ("systemctl", "--user")
    ]
    verbs = [step.argv[2] for _number, step in systemctl]
    assert verbs[:2] == ["enable", "daemon-reload"]
    assert {"start", "restart"} >= set(verbs[2:]) and "start" in verbs[2:]
    (enable_at, enable), (reload_at, reload) = systemctl[:2]
    assert max(writes) < enable_at < reload_at < min(service_starts(steps))
    assert enable.argv == ("systemctl", "--user", "enable", *unit_files)
    assert all(path.startswith("/") for path in enable.argv[3:])
    # Unconditional: a rerun after an interrupted enable still reloads before starting.
    assert reload.argv == ("systemctl", "--user", "daemon-reload") and reload.when_changed == ()
    # Start and restart address the linked units by name.
    for _number, step in systemctl[2:]:
        assert step.argv[3] in units


def test_rerun_on_an_installed_host_skips_completed_work():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files["/usr/bin/protonmail-bridge"] = ""
    probe.files[paths.venv] = ""
    probe.files[f"{paths.venv}/{DIR_MARKER}"] = MARKER
    probe.files[paths.venv_python] = ""
    probe.secrets |= {("libsecret", BRIDGE_SERVICE, USER), ("libsecret", TOKEN_SERVICE, USER)}
    steps = build("debian", probe=probe)
    argv = commands(steps)
    assert not any(isinstance(step, (Download, StoreSecret)) for step in steps)
    assert not any("venv" in command for command in argv)
    assert not any(command[:2] == ("apt-get", "install") for command in argv)
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert "already installed" in notes and "already in libsecret" in notes
    replaced = build("debian", probe=probe, replace_credentials=True)
    assert len([step for step in replaced if isinstance(step, StoreSecret)]) == 2


def test_env_file_with_the_old_send_flag_name_gets_a_note_and_keeps_the_line():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        "\n".join(templates.ENV_HEADER) + "\n"
        f"PROTON_BRIDGE_USER={USER}\n"
        "PMM_SEND_TOKEN_APPROVAL=synthetic-value\n"
    )
    steps = build("arch", probe=probe, bridge_user=None)
    assert (
        parse_env_file(written(steps, "proton-mail-mcp.env").content)["PMM_SEND_TOKEN_APPROVAL"]
        == "synthetic-value"
    )
    text = " ".join(step.text for step in steps if isinstance(step, Note))
    assert "PMM_SEND_TOKEN_APPROVAL, which is now ignored" in text
    assert "PROTON_MCP_SEND_SELF_NO_FORM=1" in text
    assert "synthetic-value" not in text


@pytest.mark.parametrize(
    "lines,warned",
    [
        ("PROTON_MCP_ENABLE_SEND=1\nPROTON_MCP_SEND_SELF_NO_FORM=1\n", True),
        ("PROTON_MCP_SEND_SELF_NO_FORM=1\n", False),
        ("PROTON_MCP_ENABLE_SEND=1\nPROTON_MCP_SEND_SELF_NO_FORM=0\n", False),
        ("PROTON_MCP_ENABLE_SEND=1\n", False),
    ],
)
def test_env_file_with_the_no_form_flag_says_the_send_from_list_receives_with_no_form(
    lines, warned
):
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        "\n".join(templates.ENV_HEADER) + "\n" + f"PROTON_BRIDGE_USER={USER}\n" + lines
    )
    steps = build("arch", probe=probe, bridge_user=None)
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert ("PROTON_MCP_SEND_FROM can receive mail with no form" in notes) is warned


@pytest.mark.parametrize(
    "lines,warned",
    [
        ("PROTON_MCP_ENABLE_SEND=1\n", True),
        ("PROTON_MCP_ENABLE_SEND=1\nPROTON_MCP_SEND_TYPED_OK=1\n", True),
        ("PROTON_MCP_ENABLE_SEND=1\nPROTON_MCP_SEND_TYPED_OK=0\n", False),
        ("PROTON_MCP_ENABLE_SEND=0\n", False),
        ("PROTON_MCP_SEND_TYPED_OK=1\n", False),
    ],
)
def test_env_file_enabling_send_says_what_a_client_with_no_form_can_do(lines, warned):
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        "\n".join(templates.ENV_HEADER) + "\n" + f"PROTON_BRIDGE_USER={USER}\n" + lines
    )
    steps = build("arch", probe=probe, bridge_user=None)
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert ("send to any recipient on a typed OK" in notes) is warned
    assert ("PROTON_MCP_SEND_TYPED_OK=0 refuses those clients" in notes) is warned


@pytest.mark.parametrize(
    "lines,warned,ignored",
    [
        ("", False, False),
        ("PROTON_MCP_SEND_TYPED_OK_CLIENTS=\n", False, False),
        ("PROTON_MCP_SEND_TYPED_OK_CLIENTS= , ,\n", False, False),
        ("PROTON_MCP_SEND_TYPED_OK_CLIENTS= Example Client ,, Other\n", True, False),
        (
            "PROTON_MCP_ENABLE_SEND=1\nPROTON_MCP_SEND_TYPED_OK_CLIENTS=Example Client,Other\n",
            True,
            False,
        ),
        (
            "PROTON_MCP_SEND_TYPED_OK=1\nPROTON_MCP_SEND_TYPED_OK_CLIENTS=Example Client,Other\n",
            True,
            False,
        ),
        (
            "PROTON_MCP_SEND_TYPED_OK=0\nPROTON_MCP_SEND_TYPED_OK_CLIENTS=Example Client,Other\n",
            False,
            True,
        ),
        ("PROTON_MCP_SEND_TYPED_OK=0\n", False, False),
    ],
)
def test_env_file_listing_typed_ok_clients_names_them_or_says_they_are_ignored(
    lines, warned, ignored
):
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        "\n".join(templates.ENV_HEADER) + "\n" + f"PROTON_BRIDGE_USER={USER}\n" + lines
    )
    steps = build("arch", probe=probe, bridge_user=None)
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert ("treats these clients as having no form: Example Client, Other." in notes) is warned
    assert ("self-reported (clientInfo.name)" in notes) is warned
    assert (
        "PROTON_MCP_SEND_TYPED_OK_CLIENTS is ignored: Example Client, Other." in notes
    ) is ignored


def test_env_file_merge_keeps_user_lines_and_reports_unsafe_choices():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        "\n".join(templates.ENV_HEADER) + "\n"
        f"PROTON_BRIDGE_USER={USER}\n"
        "PROTON_MCP_PORT=9999\n"
        "# my tailnet\n"
        "PROTON_MCP_PUBLIC_HOST=box.example.ts.net\n"
        "PROTON_MCP_CONFIRM_MUTATIONS=off-for-now\n"
        "PROTON_MCP_ENABLE_SEND=1\n"
    )
    steps = build("arch", probe=probe, bridge_user=None, port=8800)
    content = written(steps, "proton-mail-mcp.env").content
    settings = parse_env_file(content)
    assert settings["PROTON_MCP_PORT"] == "8800"
    assert settings["PROTON_MCP_PUBLIC_HOST"] == "box.example.ts.net"
    assert settings["PROTON_MCP_CONFIRM_MUTATIONS"] == "off-for-now"
    assert "# my tailnet" in content
    assert content.count(MARKER) == 1
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert "PROTON_MCP_CONFIRM_MUTATIONS to something other than 1" in notes
    assert "PROTON_MCP_ENABLE_SEND=1" in notes
    assert "PMM_SEND_TOKEN_APPROVAL" not in notes
    # Astra r7 #5: the value itself is never echoed.
    assert "off-for-now" not in notes
    probe.files[paths.env_file] = content
    assert written(build("arch", probe=probe, port=8800), ".env").content == content


def test_env_file_with_a_secret_stops_the_plan_without_echoing_it():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = "PROTON_MCP_TOKEN=leaked-token-value\n"
    with pytest.raises(PlanError) as caught:
        build("arch", probe=probe)
    assert "leaked-token-value" not in str(caught.value)


@pytest.mark.parametrize("action", [install_plan, uninstall_plan])
def test_r8_a_settings_file_the_installer_did_not_write_is_neither_used_nor_shown(action):
    # Sol r7 #3: uninstall printed the data settings of a settings file the installer
    # did not write. Such a file stops the plan before anything changes; the message
    # names its path and keys, never a value.
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        f"PROTON_MCP_ACTION_LOG=/srv/{SECRET}/actions.jsonl\nPROTON_MCP_CONFIRM_MUTATIONS={SECRET}\n"
    )
    with pytest.raises(PlanError) as refused:
        build("arch", probe=probe, action=action)
    message = str(refused.value)
    assert message.startswith(f"{paths.env_file} was not written by the installer")
    assert "(PROTON_MCP_ACTION_LOG, PROTON_MCP_CONFIRM_MUTATIONS). Nothing was changed" in message
    assert SECRET not in message


def test_bridge_user_is_required_and_validated():
    with pytest.raises(PlanError, match="--bridge-user"):
        build("arch", bridge_user=None)
    with pytest.raises(PlanError, match="quotes or slashes"):
        build("arch", bridge_user="../escape")


def test_linux_store_choice_and_prerequisites():
    platform = simulated("fedora")
    probe = simulated_probe(platform)
    del probe.tools["secret-tool"]
    with pytest.raises(PlanError, match="sudo dnf install libsecret"):
        build("fedora", probe=probe)
    probe.files["/home/you/.password-store/.gpg-id"] = "KEY\n"
    steps = build("fedora", probe=probe)
    assert env_file(steps)["PROTON_MCP_CREDENTIAL_STORE"] == "pass"
    assert {step.store for step in steps if isinstance(step, StoreSecret)} == {"pass"}
    del probe.files["/home/you/.password-store/.gpg-id"]
    with pytest.raises(PlanError, match="pass init"):
        build("fedora", probe=probe, credential_store="pass")


def pass_probe(family="debian", **env):
    platform = simulated(family)
    probe = simulated_probe(platform)
    probe.env.update(env)
    store = env.get("PASSWORD_STORE_DIR", "/home/you/.password-store")
    probe.files[f"{store}/.gpg-id"] = "KEY\n"
    return probe, paths_for(platform, probe.env, probe.home)


CUSTOM_PASS = {"PASSWORD_STORE_DIR": "/srv/pass", "GNUPGHOME": "/srv/gnupg"}


def test_a_custom_pass_store_and_gnupg_home_are_pinned_for_the_service():
    # Astra: set only in the installing shell, they never reached the service.
    probe, paths = pass_probe(**CUSTOM_PASS)
    steps = build("debian", probe=probe, credential_store="pass")
    settings = env_file(steps)
    assert settings["PROTON_MCP_PASSWORD_STORE_DIR"] == "/srv/pass"
    assert settings["PROTON_MCP_GNUPGHOME"] == "/srv/gnupg"
    stores = [step for step in steps if isinstance(step, StoreSecret)]
    assert [dict(step.locations) for step in stores] == [CUSTOM_PASS, CUSTOM_PASS]
    assert probe.secret_locations == [CUSTOM_PASS, CUSTOM_PASS]
    assert "PASSWORD_STORE_DIR=/srv/pass GNUPGHOME=/srv/gnupg pass show" in notes(steps)
    # A rerun from a shell without them keeps using the same store and keys.
    probe.files[paths.env_file] = written(steps, ".env").content
    probe.env.clear()
    probe.secret_locations.clear()
    rerun = build("debian", probe=probe)
    assert written(rerun, ".env").content == written(steps, ".env").content
    assert probe.secret_locations == [CUSTOM_PASS, CUSTOM_PASS]
    assert "changes from" not in notes(rerun)


@pytest.mark.skipif(os.name == "nt", reason="pass stores are POSIX paths")
def test_the_service_passes_the_saved_locations_to_every_pass_call(monkeypatch):
    probe, _paths = pass_probe(**CUSTOM_PASS)
    settings = env_file(build("debian", probe=probe, credential_store="pass"))
    # The service's own environment names another store, or none at all.
    monkeypatch.setenv("PASSWORD_STORE_DIR", "/home/you/another-store")
    monkeypatch.delenv("PROTON_BRIDGE_PASSWORD", raising=False)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0, "x" * 40 + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    # The token at startup, then the Bridge password the server reads later.
    get_credentials(Settings.from_env(service_entry.service_env({}, settings)))
    assert [argv[:2] for argv, _env in calls] == [["pass", "show"], ["pass", "show"]]
    for _argv, used in calls:
        assert used["PASSWORD_STORE_DIR"] == "/srv/pass"
        assert used["GNUPGHOME"] == "/srv/gnupg"


def test_the_default_pass_store_is_pinned_and_gnupg_left_to_its_default():
    probe, _paths = pass_probe()
    steps = build("debian", probe=probe, credential_store="pass")
    settings = env_file(steps)
    assert settings["PROTON_MCP_PASSWORD_STORE_DIR"] == "/home/you/.password-store"
    assert "PROTON_MCP_GNUPGHOME" not in settings
    stores = [dict(step.locations) for step in steps if isinstance(step, StoreSecret)]
    assert stores == [{"PASSWORD_STORE_DIR": "/home/you/.password-store"}] * 2


def test_libsecret_installs_write_no_pass_locations():
    probe, _paths = pass_probe(**CUSTOM_PASS)
    steps = build("debian", probe=probe, credential_store="libsecret")
    assert not {"PROTON_MCP_PASSWORD_STORE_DIR", "PROTON_MCP_GNUPGHOME"} & set(env_file(steps))
    assert all(not step.locations for step in steps if isinstance(step, StoreSecret))


def test_a_changed_pass_store_is_reported_and_relative_ones_refused():
    probe, paths = pass_probe(**CUSTOM_PASS)
    probe.files[paths.env_file] = written(
        build("debian", probe=probe, credential_store="pass"), ".env"
    ).content
    probe.env["PASSWORD_STORE_DIR"] = "/srv/other"
    probe.files["/srv/other/.gpg-id"] = "KEY\n"
    steps = build("debian", probe=probe)
    assert env_file(steps)["PROTON_MCP_PASSWORD_STORE_DIR"] == "/srv/other"
    assert "PROTON_MCP_PASSWORD_STORE_DIR changes from /srv/pass to /srv/other" in notes(steps)
    for name in CUSTOM_PASS:
        # Refused before the store is looked for under a path relative to this shell.
        relative = simulated_probe(simulated("debian"))
        relative.env.update({**CUSTOM_PASS, name: "relative/dir"})
        with pytest.raises(PlanError, match=f"{name} .* absolute"):
            build("debian", probe=relative, credential_store="pass")


def test_uninstall_deletes_pass_entries_where_the_service_kept_them():
    probe, paths = pass_probe(**CUSTOM_PASS)
    probe.files[paths.env_file] = written(
        build("debian", probe=probe, credential_store="pass"), ".env"
    ).content
    probe.env["PASSWORD_STORE_DIR"] = "/srv/unrelated"
    steps = build("debian", probe=probe, action=uninstall_plan, remove_credentials=True)
    deletes = [step for step in steps if isinstance(step, DeleteSecret)]
    assert [dict(step.locations) for step in deletes] == [CUSTOM_PASS, CUSTOM_PASS]


@pytest.mark.parametrize(
    ("family", "store"), [("macos", "libsecret"), ("windows", "pass"), ("arch", "wincred")]
)
def test_stores_must_match_the_platform(family, store):
    with pytest.raises(PlanError, match="credential store"):
        build(family, credential_store=store)


def test_skip_flags_limit_the_plan():
    steps = build("debian", skip_bridge=True, skip_credentials=True, skip_bridge_service=True)
    argv = commands(steps)
    assert not any(command[0] in {"apt-get", "debsig-verify"} for command in argv)
    assert not any(isinstance(step, StoreSecret) for step in steps)
    assert "Wants=" not in written(steps, templates.MCP_UNIT).content
    assert not any(
        isinstance(step, WriteFile) and templates.BRIDGE_UNIT in step.path for step in steps
    )
    services = build("debian", skip_services=True)
    assert not any(command[0] == "systemctl" for command in commands(services))


def test_skip_bridge_with_a_bridge_service_needs_an_existing_binary():
    with pytest.raises(PlanError, match="--bridge-bin"):
        build("arch", skip_bridge=True)
    probe = simulated_probe(simulated("arch"))
    probe.files["/opt/bridge/bin/bridge"] = ""
    steps = build("arch", probe=probe, skip_bridge=True, bridge_bin="/opt/bridge/bin/bridge")
    assert (
        "ExecStart=/opt/bridge/bin/bridge --noninteractive"
        in written(steps, templates.BRIDGE_UNIT).content
    )


@pytest.mark.parametrize("port", [0, 70000])
def test_ports_are_validated(port):
    with pytest.raises(PlanError, match="--port"):
        build("arch", port=port)


def test_unsupported_platforms_are_refused():
    platform = Platform("unsupported", "alpine", "3", "x86_64", "Alpine")
    for action in (install_plan, uninstall_plan, install_steps, uninstall_steps):
        with pytest.raises(PlanError, match="Unsupported"):
            build("arch", platform=platform, probe=FakeProbe(), action=action)


@pytest.mark.parametrize("action", [install_plan, uninstall_plan])
def test_windows_is_refused_until_the_server_runs_there(action):
    platform = simulated("windows")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    with pytest.raises(PlanError) as caught:
        action(platform, options(), probe, paths)
    assert str(caught.value).startswith("Windows is not yet supported")
    assert "fcntl" in str(caught.value)


@pytest.mark.parametrize("family", FAMILIES)
def test_uninstall_keeps_state_credentials_and_bridge_by_default(family):
    platform = simulated(family)
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    steps = build(family, probe=probe, action=uninstall_plan)
    removed = [step for step in steps if isinstance(step, RemovePath)]
    assert not any(isinstance(step, DeleteSecret) for step in steps)
    assert not any(paths.state_dir in step.path for step in removed)
    # Round 7: files must carry the marker, a directory goes only once empty, and the
    # venv and the downloads are never removed.
    assert all(step.managed_only for step in removed)
    assert not {paths.venv, paths.cache_dir} & {step.path for step in removed}
    removal = {"apt-get", "dnf", "pacman", "winget"}
    assert not any(argv[0] in removal or "uninstall" in argv for argv in commands(steps))
    notes = " ".join(step.text for step in steps if isinstance(step, Note))
    assert paths.state_dir in notes


@pytest.mark.parametrize(
    ("family", "argv"),
    [
        ("debian", ("apt-get", "remove", "-y", "protonmail-bridge")),
        ("fedora", ("dnf", "remove", "-y", "protonmail-bridge")),
        ("arch", ("pacman", "-R", "--noconfirm", "protonmail-bridge-core")),
        ("macos", ("/opt/homebrew/bin/brew", "uninstall", "--cask", "proton-mail-bridge")),
        ("windows", ("winget", "uninstall", "--id", "Proton.ProtonMailBridge", "--exact")),
    ],
)
def test_uninstall_can_remove_credentials_and_bridge_on_request(family, argv):
    steps = build(family, action=uninstall_plan, remove_credentials=True, remove_bridge=True)
    deletions = [(step.service, step.account) for step in steps if isinstance(step, DeleteSecret)]
    assert deletions == [(BRIDGE_SERVICE, USER), (TOKEN_SERVICE, USER)]
    assert any(command[: len(argv)] == argv for command in commands(steps))


def test_uninstall_reads_the_account_from_the_env_file():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        f"# {MARKER}\nPROTON_BRIDGE_USER=saved@example.com\nPROTON_MCP_CREDENTIAL_STORE=pass\n"
    )
    probe.files["/home/you/.password-store/.gpg-id"] = "KEY\n"
    steps = build(
        "arch", probe=probe, action=uninstall_plan, bridge_user=None, remove_credentials=True
    )
    assert {(step.store, step.account) for step in steps if isinstance(step, DeleteSecret)} == {
        ("pass", "saved@example.com")
    }


def test_linux_uninstall_disables_only_present_units():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[f"{paths.unit_dir}/{templates.MCP_UNIT}"] = MARKER
    steps = build("debian", probe=probe, action=uninstall_plan)
    disable = next(step for step in steps if isinstance(step, Command) and "disable" in step.argv)
    assert disable.argv == ("systemctl", "--user", "disable", "--now", templates.MCP_UNIT)
    # Its exit status proves nothing (Sol r6 #4); the check right after it does.
    assert disable.allow_failure
    (check,) = [step for step in steps if isinstance(step, CheckStopped)]
    assert steps.index(check) == steps.index(disable) + 1
    assert check.argv == (
        "systemctl",
        "--user",
        "show",
        "--property=ActiveState",
        "--value",
        templates.MCP_UNIT,
    )
    assert check.states == ("inactive", "failed")


def test_installer_owned_directories_are_marked():
    steps = build("debian")
    markers = [
        step.path
        for step in steps
        if isinstance(step, WriteFile) and step.path.endswith(DIR_MARKER)
    ]
    assert markers == [
        f"/home/you/.cache/proton-mail-mcp/installer/{DIR_MARKER}",
        f"/home/you/.local/share/proton-mail-mcp/venv/{DIR_MARKER}",
    ]


def test_xdg_directories_are_honoured_only_when_absolute():
    platform = simulated("arch")
    paths = paths_for(platform, {"XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "relative"}, "/home/u")
    assert paths.env_file == "/cfg/proton-mail-mcp/proton-mail-mcp.env"
    assert paths.unit_dir == "/cfg/systemd/user"
    assert paths.venv == "/home/u/.local/share/proton-mail-mcp/venv"


# A pre-existing virtual environment is never claimed (Sol: venv ownership).


@pytest.mark.parametrize("present", ["directory", "python"])
def test_an_existing_venv_without_the_marker_is_refused(present):
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.venv] = ""
    if present == "python":
        probe.files[paths.venv_python] = ""
    with pytest.raises(PlanError) as caught:
        build("debian", probe=probe)
    message = str(caught.value)
    assert paths.venv in message and "not created by this installer" in message
    assert "remove it yourself" in message and "--venv" in message


def test_the_marker_is_written_before_the_venv_exists_and_reruns_reuse_it():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    marker = f"{paths.venv}/{DIR_MARKER}"
    steps = build("arch", probe=probe)
    claim = index(steps, lambda s: isinstance(s, WriteFile) and s.path == marker)
    create = index(steps, lambda s: isinstance(s, Command) and s.argv[1:3] == ("-m", "venv"))
    assert claim < create
    probe.files.update({paths.venv: "", marker: MARKER, paths.venv_python: ""})
    again = build("arch", probe=probe)
    assert not any(isinstance(s, Command) and "venv" in s.argv for s in again)
    assert any(isinstance(s, WriteFile) and s.path == marker for s in again)


def test_an_unmarked_download_directory_is_refused():
    platform = simulated("fedora")
    probe = simulated_probe(platform)
    probe.files["/home/you/.cache/proton-mail-mcp/installer"] = ""
    with pytest.raises(PlanError, match="XDG_CACHE_HOME"):
        build("fedora", probe=probe)


def test_venv_option_relocates_the_environment_and_is_remembered():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    steps = build("debian", probe=probe, venv="/opt/pmm/venv")
    assert env_file(steps)[VENV_KEY] == "/opt/pmm/venv"
    assert ("/opt/pmm/venv/bin/python", "-m", "pip") == next(
        argv[:3] for argv in commands(steps) if "pip" in argv
    )
    assert "ExecStart=/opt/pmm/venv/bin/proton-mail-mcp-service " in (
        written(steps, templates.MCP_UNIT).content
    )
    assert not any(paths.venv in str(step) for step in steps)
    probe.files[paths.env_file] = written(steps, ".env").content
    rerun = build("debian", probe=probe)
    assert written(rerun, ".env").content == written(steps, ".env").content
    assert any(isinstance(s, WriteFile) and s.path == f"/opt/pmm/venv/{DIR_MARKER}" for s in rerun)
    # The previous environment is not on this host: nothing to name or remember.
    moved = build("debian", probe=probe, venv="/srv/other-venv")
    assert "/opt/pmm/venv" not in notes(moved) and not directory_removals(moved)
    assert PREVIOUS_VENVS_KEY not in env_file(moved)
    # Uninstall finds the saved environment, and keeps it (round 7). No MCP unit of
    # the installer's was stopped, so nothing proves the venv unused.
    probe.files.update({"/opt/pmm/venv": "", f"/opt/pmm/venv/{DIR_MARKER}": MARKER})
    removal = build("debian", probe=probe, action=uninstall_plan)
    assert not [s for s in removal if isinstance(s, RemovePath) and "venv" in s.path]
    assert (
        "The virtual environment /opt/pmm/venv (36 bytes) stays: the installer stopped no MCP "
        "service of its own, so one of yours may still run from it."
    ) in notes(removal)
    with pytest.raises(PlanError, match="absolute"):
        build("debian", venv="relative/venv")


def moved_venv_probe(family, old="/opt/pmm/venv", marked=True):
    """A host installed with --venv old, whose environment exists."""
    platform = simulated(family)
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = written(build(family, probe=probe, venv=old), ".env").content
    probe.files[old] = ""
    if marked:
        separator = "\\" if family == "windows" else "/"
        probe.files[f"{old}{separator}{DIR_MARKER}"] = MARKER
    return probe, paths


def directory_removals(steps):
    return [step for step in steps if isinstance(step, RemovePath) and step.directory]


def service_starts(steps):
    return [
        number
        for number, step in enumerate(steps)
        if isinstance(step, (Command, WaitForPort))
        and (isinstance(step, WaitForPort) or {"start", "restart", "bootstrap"} & set(step.argv))
    ]


# Round 7: print, don't delete. A previous environment stays, every byte of it, and a
# note (printed, and logged when applied) gives its size and, where that cannot
# obviously harm this installation, the command that removes it by hand.

OLD, NEW = "/opt/pmm/venv", "/srv/new-venv"
DEFAULT_ATTACHMENTS = "/home/you/.local/share/proton-mail-mcp/attachments"


def kept_notes(steps):
    """The notes on directories that stay; they are also logged."""
    return [step for step in steps if isinstance(step, Note) and step.log]


def kept_note(steps, path):
    (note,) = [step for step in kept_notes(steps) if f" {path} (" in step.text]
    return note


def mentions(steps, text):
    """Every step but the notes whose fields mention text."""
    return [step for step in steps if not isinstance(step, Note) and text in repr(step)]


@pytest.mark.parametrize("family", SUPPORTED)
def test_r7_moving_the_venv_keeps_the_previous_one_and_prints_its_size_and_command(family):
    # R1: no installer step removes a directory with content. Sol and Astra found a
    # delete-path gap in every round since 3; the previous venv now always stays.
    probe, paths = moved_venv_probe(family)
    probe.files[f"{OLD}/lib/site.py"] = "x" * 2000
    steps = build(family, probe=probe, venv=NEW)
    assert not directory_removals(steps)
    assert not any(isinstance(step, RemovePath) and OLD in step.path for step in steps)
    note = kept_note(steps, OLD)
    assert note.text == (
        f"The previous virtual environment {OLD} (2.0 KB) stays. The installer never deletes "
        "it and has not checked whether anything else of yours runs from it, keeps data in "
        f"it or links into it. Once nothing does, remove it with: `rm -rf -- {OLD}`"
    )
    assert note.log == f"{paths.state_dir}/installer.log"
    assert steps.index(note) > max(service_starts(steps))
    # Only the settings still name it, in the list of environments left in place.
    (settings_file,) = mentions(steps, OLD)
    assert settings_file.path == paths.env_file
    settings = env_file(steps)
    assert settings[VENV_KEY] == NEW and settings[PREVIOUS_VENVS_KEY] == f'["{OLD}"]'
    assert [key for key, value in settings.items() if OLD in value] == [PREVIOUS_VENVS_KEY]
    service = written(steps, templates.MCP_UNIT if family != "macos" else "mcp.plist").content
    assert f"{NEW}/bin/proton-mail-mcp-service" in service and OLD not in service
    # Nothing is stopped or moved for it.
    assert not any(isinstance(step, Command) and "stop" in step.argv for step in steps)
    assert "uninstall --venv" not in notes(steps)


def test_r7_moving_away_from_the_default_venv_keeps_it():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files.update({paths.venv: "", f"{paths.venv}/{DIR_MARKER}": MARKER})
    steps = build("arch", probe=probe, venv=NEW)
    assert not directory_removals(steps)
    assert kept_note(steps, paths.venv).text.endswith(f"remove it with: `rm -rf -- {paths.venv}`")
    assert env_file(steps)[PREVIOUS_VENVS_KEY] == f'["{paths.venv}"]'
    # A plain rerun in place leaves nothing behind to name.
    again = build("arch", probe=probe)
    assert not kept_notes(again) and PREVIOUS_VENVS_KEY not in env_file(again)


@pytest.mark.parametrize(
    "case",
    [
        "symlink",
        "new-inside-old",
        "old-inside-new",
        "old-inside-linked-new",
        "state-dir-link",
        "bridge-inside",
        "bridge-dot-dot",
        "bridge-link-into-old",
        "bridge-link-in-old",
        "data-setting",
        "data-setting-names-it",
        "gnupg-default-home",
        "pass-default-store",
        "skip-services",
    ],
)
def test_r7_rule6_no_removal_command_where_it_could_harm_this_installation(case):
    # R6: a command only for a marked, real directory that does not overlap the
    # current venv and holds nothing this installation uses, by name or resolved; for
    # a previous venv only once the services were switched away from it.
    probe, paths = moved_venv_probe("debian")
    new, extra = NEW, {}
    used = "still holds what this installation uses: "
    if case == "symlink":
        probe.links[OLD] = "/mnt/ssd/venv"
        probe.files.update({"/mnt/ssd/venv": "", f"/mnt/ssd/venv/{DIR_MARKER}": MARKER})
        reason = f"The previous virtual environment {OLD} (size unknown) stays: it is a symbolic"
    elif case == "new-inside-old":
        new = f"{OLD}/inner"
        reason = f"stays: it holds the current virtual environment {new}."
    elif case == "old-inside-new":
        new = "/opt/pmm"
        reason = "stays: it lies inside the current virtual environment /opt/pmm."
    elif case == "old-inside-linked-new":
        new = "/srv/link"
        probe.links["/srv/link"] = "/opt"
        reason = "stays: it lies inside the current virtual environment /srv/link."
    elif case == "state-dir-link":
        probe.links["/home/you/.local/state"] = f"{OLD}/state"
        reason = f"{used}the state directory, PROTON_MCP_ACTION_LOG, PROTON_MCP_IDENTITY_INDEX_DIR."
    elif case.startswith("bridge-"):
        bridge, links = {
            "bridge-inside": (f"{OLD}/bin/bridge", {}),
            "bridge-dot-dot": ("/opt/pmm/other/../venv/bin/bridge", {}),
            "bridge-link-into-old": (
                "/usr/local/bin/bridge",
                {"/usr/local/bin/bridge": f"{OLD}/bin/bridge"},
            ),
            "bridge-link-in-old": (f"{OLD}/linked/bridge", {f"{OLD}/linked": "/usr/lib/bridge"}),
        }[case]
        probe.links.update(links)
        probe.files[bridge] = ""
        extra = {"bridge_bin": bridge}
        reason = f"{used}the Bridge executable."
    elif case == "data-setting":
        with_settings(probe, paths, PROTON_MCP_SAVED_RULES=f"{OLD}/rules.json")
        reason = f"{used}PROTON_MCP_SAVED_RULES."
    elif case == "data-setting-names-it":
        # Sol r7 #6: a setting equal to the directory itself lost its protection.
        with_settings(probe, paths, PROTON_MCP_IDENTITY_INDEX_DIR=OLD)
        reason = f"{used}PROTON_MCP_IDENTITY_INDEX_DIR."
    elif case == "gnupg-default-home":
        probe.links["/home/you/.gnupg"] = f"{OLD}/gnupg"
        reason = f"{used}GnuPG's default home."
    elif case == "pass-default-store":
        probe.links["/home/you/.password-store"] = f"{OLD}/pass"
        reason = f"{used}pass's default store."
    else:
        extra = {"skip_services": True}
        reason = (
            f"stays: with --skip-services (or without systemd) no service was switched to "
            f"{NEW}, so services may still run from it."
        )
    steps = build("debian", probe=probe, venv=new, **extra)
    assert not directory_removals(steps)
    text = kept_note(steps, OLD).text
    assert reason in text, text
    assert "The installer never deletes it" in text and "rm -rf" not in text


def test_the_same_venv_under_another_name_is_not_a_move():
    probe, _paths = moved_venv_probe("debian")
    for alias, links in (
        ("/opt/pmm/x/../venv", {}),
        ("/srv/alias/venv", {"/srv/alias": "/opt/pmm"}),
    ):
        probe.links = links
        steps = build("debian", probe=probe, venv=alias)
        assert not directory_removals(steps)
        assert "previous" not in notes(steps)
        assert PREVIOUS_VENVS_KEY not in env_file(steps)


def test_r7_previous_venvs_are_named_every_run_and_never_retried():
    # The list now means: environments the installer created and left in place.
    probe, paths = moved_venv_probe("debian")
    first = build("debian", probe=probe, venv=NEW)
    assert env_file(first)[PREVIOUS_VENVS_KEY] == f'["{OLD}"]'
    # A rerun names it again, as a reminder; nothing moves or goes.
    probe.files[paths.env_file] = written(first, ".env").content
    probe.files.update({NEW: "", f"{NEW}/{DIR_MARKER}": MARKER})
    again = build("debian", probe=probe)
    assert kept_note(again, OLD).text == kept_note(first, OLD).text
    assert env_file(again)[PREVIOUS_VENVS_KEY] == f'["{OLD}"]' and not directory_removals(again)
    # A further move adds the next one.
    third = "/srv/third-venv"
    steps = build("debian", probe=probe, venv=third)
    assert env_file(steps)[PREVIOUS_VENVS_KEY] == f'["{OLD}", "{NEW}"]'
    assert kept_note(steps, OLD) and kept_note(steps, NEW)
    # Removed by hand, one is forgotten; once none is left, the list is cleared.
    probe.files[paths.env_file] = written(steps, ".env").content
    probe.files.update({third: "", f"{third}/{DIR_MARKER}": MARKER})
    del probe.files[OLD]
    assert env_file(build("debian", probe=probe))[PREVIOUS_VENVS_KEY] == f'["{NEW}"]'
    del probe.files[NEW]
    assert env_file(build("debian", probe=probe))[PREVIOUS_VENVS_KEY] == ""


def test_an_unmarked_previous_venv_is_not_remembered():
    probe, _paths = moved_venv_probe("debian", marked=False)
    steps = build("debian", probe=probe, venv=NEW)
    assert PREVIOUS_VENVS_KEY not in env_file(steps)
    assert not kept_notes(steps)


@pytest.mark.parametrize("value", ["/opt/pmm/venv", '["relative/venv"]', "[1]"])
def test_a_malformed_list_of_previous_venvs_stops_the_plan(value):
    probe, paths = moved_venv_probe("debian")
    with_settings(probe, paths, **{PREVIOUS_VENVS_KEY: value})
    with pytest.raises(PlanError, match="must be a JSON list of absolute paths"):
        build("debian", probe=probe)


DATA_IN_OLD = {
    "PROTON_MCP_ACTION_LOG": f"{OLD}/audit/actions.jsonl",
    "PROTON_MCP_IDENTITY_INDEX_DIR": f"{OLD}/identity-index",
    "PROTON_MCP_SAVED_RULES": f"{OLD}/rules/custom.json",
    "PROTON_MCP_PROTECTED_CONFIG": f"{OLD}/protected.json",
    "PROTON_BRIDGE_CERT": f"{OLD}/bridge-cert.pem",
}


def with_settings(probe, paths, **values):
    """Set env file values on a simulated host, replacing earlier lines for those keys."""
    lines = [
        line
        for line in probe.files[paths.env_file].splitlines()
        if line.partition("=")[0] not in values
    ]
    probe.files[paths.env_file] = "\n".join([*lines, *(f"{k}={v}" for k, v in values.items())])


def mcp_service_running(family, probe, paths):
    """The installer's MCP unit or agent is in place (and loaded, on macOS)."""
    if family == "macos":
        plist = f"{paths.agents_dir}/{templates.MCP_LABEL}.plist"
        probe.files[plist] = f"<!-- {MARKER} -->"
        command = ("launchctl", "print", f"gui/{probe.user_id}/{templates.MCP_LABEL}")
        probe.outputs[command] = f"    path = {plist}\n"
    else:
        probe.files[f"{paths.unit_dir}/{templates.MCP_UNIT}"] = f"# {MARKER}\n"


@pytest.mark.parametrize("family", SUPPORTED)
def test_r7_data_in_the_previous_venv_stays_there_and_its_settings_still_point_there(family):
    # Sol (rounds 3-6): data moved out of the old venv before it was removed, and each
    # round found a use the checks missed. Round 7 moves nothing and removes nothing.
    probe, paths = moved_venv_probe(family)
    with_settings(probe, paths, **DATA_IN_OLD)
    mcp_service_running(family, probe, paths)
    steps = build(family, probe=probe, venv=NEW)
    settings = env_file(steps)
    assert {key: settings[key] for key in DATA_IN_OLD} == DATA_IN_OLD
    assert not directory_removals(steps)
    assert not any(isinstance(step, Command) and "stop" in step.argv for step in steps)
    text = kept_note(steps, OLD).text
    assert f"still holds what this installation uses: {', '.join(DATA_IN_OLD)}." in text
    assert "Move those out (and update any setting that names them)" in text
    assert "rm -rf" not in text
    assert f"Audit and undo logs: {OLD}/audit/actions.jsonl" in notes(steps)


def test_r7_a_pass_store_and_gnupg_home_in_the_previous_venv_stay_in_use():
    # Astra (round 3): PASSWORD_STORE_DIR and GNUPGHOME inside the old venv went with it.
    # They stay in use where they are; secrets are looked up and stored there.
    probe, paths = moved_venv_probe("debian")
    with_settings(
        probe,
        paths,
        PROTON_MCP_CREDENTIAL_STORE="pass",
        PROTON_MCP_PASSWORD_STORE_DIR=f"{OLD}/pass",
        PROTON_MCP_GNUPGHOME=f"{OLD}/gnupg",
    )
    probe.files[f"{OLD}/pass/.gpg-id"] = "KEYID\n"
    probe.secrets = {("pass", BRIDGE_SERVICE, USER)}
    probe.secret_locations.clear()
    steps = build("debian", probe=probe, venv=NEW)
    old_locations = {"PASSWORD_STORE_DIR": f"{OLD}/pass", "GNUPGHOME": f"{OLD}/gnupg"}
    assert probe.secret_locations == [old_locations, old_locations]
    (stored,) = [step for step in steps if isinstance(step, StoreSecret)]
    assert stored.service == TOKEN_SERVICE and stored.locations == tuple(old_locations.items())
    settings = env_file(steps)
    assert settings["PROTON_MCP_PASSWORD_STORE_DIR"] == f"{OLD}/pass"
    assert settings["PROTON_MCP_GNUPGHOME"] == f"{OLD}/gnupg"
    assert f"PASSWORD_STORE_DIR={OLD}/pass GNUPGHOME={OLD}/gnupg pass show" in notes(steps)
    text = kept_note(steps, OLD).text
    assert "uses: PROTON_MCP_PASSWORD_STORE_DIR, PROTON_MCP_GNUPGHOME." in text
    assert "rm -rf" not in text


@pytest.mark.parametrize(
    ("key", "name", "saved", "store"),
    [
        ("PROTON_MCP_GNUPGHOME", "GNUPGHOME", f"{OLD}/gnupg", "pass"),
        ("PROTON_MCP_ACTION_LOG", "PROTON_MCP_ACTION_LOG", f"{OLD}/audit/actions.jsonl", None),
    ],
    ids=["gnupg-home", "action-log"],
)
def test_r7_a_location_the_shell_replaces_keeps_the_previous_venv_without_a_command(
    key, name, saved, store
):
    # Astra r6 #1: a saved GnuPG home lost its protection once the shell named another.
    # The shell value still wins and nothing moves, but the old place still counts as
    # something the previous venv holds, so its note suggests no removal.
    probe, paths = moved_venv_probe("debian")
    values = {key: saved}
    if store:
        values.update(PROTON_MCP_CREDENTIAL_STORE=store, PROTON_MCP_PASSWORD_STORE_DIR="/srv/pass")
        probe.files["/srv/pass/.gpg-id"] = "KEYID\n"
    with_settings(probe, paths, **values)
    probe.env[name] = "/srv/elsewhere"
    probe.files[saved] = "data"
    steps = build("debian", probe=probe, venv=NEW)
    assert env_file(steps)[key] == "/srv/elsewhere"
    assert f"{key} changes from {saved} to /srv/elsewhere" in notes(steps)
    text = kept_note(steps, OLD).text
    assert f"still holds what this installation uses: where {key} pointed before this run." in (
        text
    )
    assert "rm -rf" not in text and not directory_removals(steps)


def bridge_unit(*lines):
    return "\n".join(["[Unit]", "Description=Bridge, as the user left it", "[Service]", *lines, ""])


@pytest.mark.parametrize(
    ("old", "lines", "env"),
    [
        (
            "/home/you/.cache/old-venv",
            ["ExecStart=/bin/sh %C/old-venv/start.sh"],
            {"XDG_CACHE_HOME": "/tmp/install-cache"},
        ),
        ("/home/you/venv", ["ExecStart=/bin/sh venv/start-bridge.sh"], {}),
        ("/home/you/venv", ["Environment=X=venv", "ExecStart=/bin/sh $X/start.sh"], {}),
        ("/opt/old-venv", ["ExecStart=/opt/\\u006fld-venv/start.sh"], {}),
    ],
    ids=["sol1-xdg-specifier", "sol2-relative-argument", "sol2-relative-variable", "sol3-escape"],
)
def test_r7_sol_unit_values_never_cost_the_old_venv(old, lines, env):
    # Sol r6 #1-#3: %C resolved from the shell's XDG_CACHE_HOME, relative arguments and
    # variables, and \uNNNN escapes slipped past the unit parser, and the old venv was
    # deleted under a Bridge unit left in place. The parser is gone, and so is the
    # deletion: only notes and the list of environments left in place name it.
    probe, paths = moved_venv_probe("debian", old=old)
    probe.env.update(env)
    probe.files[f"{paths.unit_dir}/{templates.BRIDGE_UNIT}"] = bridge_unit(*lines)
    probe.files[f"{old}/start.sh"] = "#!/bin/sh\n"
    steps = build("debian", probe=probe, venv=NEW, skip_bridge_service=True)
    (settings_file,) = mentions(steps, old)
    assert settings_file.path == paths.env_file
    assert env_file(steps)[PREVIOUS_VENVS_KEY] == f'["{old}"]'
    text = kept_note(steps, old).text
    assert text.startswith(f"The previous virtual environment {old} (46 bytes) stays. ")
    assert "has not checked whether anything else of yours runs from it" in text
    assert text.endswith(f"remove it with: `rm -rf -- {old}`")


# Uninstall keeps the venv and the downloads and prints how to remove them (round 7).


def uninstall_probe(**values):
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    settings = {
        "PROTON_BRIDGE_USER": USER,
        "PROTON_MCP_CREDENTIAL_STORE": "pass",
        VENV_KEY: OLD,
        **values,
    }
    probe.files[paths.env_file] = templates.render_env_file(None, settings, {})
    probe.files.update(
        {
            OLD: "",
            f"{OLD}/{DIR_MARKER}": MARKER,
            paths.cache_dir: "",
            f"{paths.cache_dir}/{DIR_MARKER}": MARKER,
            # The installer's MCP unit, which uninstall stops and proves stopped.
            f"{paths.unit_dir}/{templates.MCP_UNIT}": f"# {MARKER}\n",
        }
    )
    return probe, paths


def test_r7_uninstall_keeps_the_venv_and_the_downloads_and_prints_how_to_remove_them():
    # Sol r6 #5: uninstall removed the current venv while a service it left running
    # used it. Neither the venv nor the downloads are removed now.
    probe, base = uninstall_probe()
    probe.files[f"{OLD}/bin/python"] = "x" * 1500
    steps = build("debian", probe=probe, action=uninstall_plan)
    removed = [step.path for step in steps if isinstance(step, RemovePath)]
    assert OLD not in removed and base.cache_dir not in removed and base.env_file in removed
    assert [step.path for step in directory_removals(steps)] == [base.config_dir, base.data_dir]
    venv, cache = kept_note(steps, OLD), kept_note(steps, base.cache_dir)
    assert venv.text.startswith(f"The virtual environment {OLD} (1.5 KB) stays. ")
    assert venv.text.endswith(f"remove it with: `rm -rf -- {OLD}`")
    assert cache.text.startswith(f"The download directory {base.cache_dir} (36 bytes) stays. ")
    assert cache.text.endswith(f"remove it with: `rm -rf -- {base.cache_dir}`")
    assert {venv.log, cache.log} == {f"{base.state_dir}/installer.log"}
    assert "; the settings file is removed." in notes(steps)


def test_r7_uninstall_gives_no_command_for_a_directory_that_holds_data():
    # Sol and Astra (rounds 3-6): uninstall moved data out and removed the venv and the
    # downloads, losing what a check missed. Both stay; the notes name what they hold,
    # and the closing note says where every data setting points.
    cache = "/home/you/.cache/proton-mail-mcp/installer"
    probe, base = uninstall_probe(
        PROTON_MCP_ACTION_LOG=f"{OLD}/audit/actions.jsonl",
        PROTON_MCP_PASSWORD_STORE_DIR=f"{OLD}/pass",
        PROTON_MCP_GNUPGHOME=f"{OLD}/gnupg",
        PROTON_MCP_PROTECTED_CONFIG=f"{cache}/protected.json",
    )
    assert base.cache_dir == cache
    steps = build("debian", probe=probe, action=uninstall_plan, remove_credentials=True)
    venv, downloads = kept_note(steps, OLD).text, kept_note(steps, cache).text
    assert (
        "still holds what this installation uses: PROTON_MCP_ACTION_LOG, "
        "PROTON_MCP_PASSWORD_STORE_DIR, PROTON_MCP_GNUPGHOME."
    ) in venv
    assert "still holds what this installation uses: PROTON_MCP_PROTECTED_CONFIG." in downloads
    assert "rm -rf" not in venv + downloads
    # Secrets are deleted where the service kept them; then the settings file goes.
    deleted = [step for step in steps if isinstance(step, DeleteSecret)]
    assert {step.locations for step in deleted} == {
        (("PASSWORD_STORE_DIR", f"{OLD}/pass"), ("GNUPGHOME", f"{OLD}/gnupg"))
    }
    settings_removed = index(steps, lambda s: isinstance(s, RemovePath) and s.path == base.env_file)
    assert max(map(steps.index, deleted)) < settings_removed
    assert (
        f"Your data stays where these settings put it: PROTON_MCP_ACTION_LOG={OLD}/audit/"
        f"actions.jsonl, PROTON_MCP_IDENTITY_INDEX_DIR={base.state_dir}/identity-index, "
        f"PROTON_MCP_ATTACHMENTS_DIR={DEFAULT_ATTACHMENTS}, "
        f"PROTON_MCP_PROTECTED_CONFIG={cache}/protected.json, PROTON_MCP_PASSWORD_STORE_DIR="
        f"{OLD}/pass, PROTON_MCP_GNUPGHOME={OLD}/gnupg; the settings file is removed."
    ) in notes(steps)


@pytest.mark.parametrize(
    "values, shown",
    [
        ({}, DEFAULT_ATTACHMENTS),
        ({"PROTON_MCP_ATTACHMENTS_DIR": ""}, DEFAULT_ATTACHMENTS),
        ({"PROTON_MCP_ATTACHMENTS_DIR": "/srv/files"}, "/srv/files"),
    ],
    ids=["unset", "empty", "set"],
)
def test_uninstall_says_where_saved_attachments_stay(values, shown):
    probe, _paths = uninstall_probe(**values)
    closing = notes(build("debian", probe=probe, action=uninstall_plan))
    assert f"PROTON_MCP_ATTACHMENTS_DIR={shown}; " in closing
    assert closing.count("PROTON_MCP_ATTACHMENTS_DIR=") == 1


def test_uninstall_names_no_attachments_folder_without_a_usable_home(monkeypatch):
    monkeypatch.setattr(planner, "default_attachments_dir", lambda _env: None)
    probe, _paths = uninstall_probe()
    assert "PROTON_MCP_ATTACHMENTS_DIR" not in notes(
        build("debian", probe=probe, action=uninstall_plan)
    )


def test_a_previous_venv_holding_the_default_attachments_folder_gets_no_command(monkeypatch):
    # The settings file never names this folder, so only the plan knows of it.
    monkeypatch.setattr(planner, "default_attachments_dir", lambda _env: f"{OLD}/files")
    probe, paths = moved_venv_probe("debian")
    mcp_service_running("debian", probe, paths)
    text = kept_note(build("debian", probe=probe, venv=NEW), OLD).text
    assert "still holds what this installation uses: the default attachments folder." in text
    assert "rm -rf" not in text


def test_uninstall_names_the_default_attachments_folder_once_in_a_kept_directory(monkeypatch):
    monkeypatch.setattr(planner, "default_attachments_dir", lambda _env: f"{OLD}/files")
    probe, _paths = uninstall_probe()
    text = kept_note(build("debian", probe=probe, action=uninstall_plan), OLD).text
    assert "still holds what this installation uses: PROTON_MCP_ATTACHMENTS_DIR." in text


def test_uninstall_names_previous_venvs_it_leaves_with_their_data():
    probe, _paths = uninstall_probe(**{PREVIOUS_VENVS_KEY: '["/srv/older-venv", "/srv/gone"]'})
    probe.files.update({"/srv/older-venv": "", f"/srv/older-venv/{DIR_MARKER}": MARKER})
    steps = build("debian", probe=probe, action=uninstall_plan)
    assert "/srv/gone" not in notes(steps)
    assert "/srv/older-venv" not in [step.path for step in steps if isinstance(step, RemovePath)]
    text = kept_note(steps, "/srv/older-venv").text
    assert text.startswith("The previous virtual environment /srv/older-venv (36 bytes) stays.")
    assert text.endswith("remove it with: `rm -rf -- /srv/older-venv`")


@pytest.mark.parametrize("case", ["unmarked-venv", "symlinked-downloads"])
def test_r7_rule6_uninstall_suggests_no_command_for_what_it_cannot_vouch_for(case):
    probe, base = uninstall_probe()
    if case == "unmarked-venv":
        del probe.files[f"{OLD}/{DIR_MARKER}"]
        path = OLD
        reason = f"stays: the installer did not create it (it has no {DIR_MARKER} marker)."
    else:
        probe.links[base.cache_dir] = "/mnt/cache"
        path = base.cache_dir
        reason = "(size unknown) stays: it is a symbolic link."
    steps = build("debian", probe=probe, action=uninstall_plan)
    text = kept_note(steps, path).text
    assert reason in text and "rm -rf" not in text
    assert path not in [step.path for step in steps if isinstance(step, RemovePath)]


@pytest.mark.parametrize(
    ("family", "bridge"),
    [("debian", "bin/bridge"), ("macos", "./Bridge"), ("windows", "bridge\\proton-bridge.exe")],
)
def test_a_relative_bridge_executable_is_refused(family, bridge):
    # Sol: ExecStart=./bin/bridge passes the existence check, and systemd rejects it.
    probe = simulated_probe(simulated(family))
    probe.files[bridge] = ""
    with pytest.raises(PlanError, match="--bridge-bin must be an absolute path"):
        build(family, probe=probe, skip_bridge=True, bridge_bin=bridge)


# Services are stopped, disabled, unloaded or deleted only once proven ours (Sol).


def test_linux_uninstall_never_stops_units_it_did_not_write():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[f"{paths.unit_dir}/{templates.MCP_UNIT}"] = f"# {MARKER}\n"
    probe.outputs[unit_fragment(templates.MCP_UNIT)] = f"{paths.unit_dir}/{templates.MCP_UNIT}\n"
    steps = build("debian", probe=probe, action=uninstall_plan)
    disable = [argv for argv in commands(steps) if "disable" in argv]
    assert disable == [("systemctl", "--user", "disable", "--now", templates.MCP_UNIT)]
    # A unit under the installer's name that it did not write, here or loaded from
    # elsewhere, stops uninstall before anything is stopped (round 8).
    for path, loaded in (
        (f"{paths.unit_dir}/{templates.BRIDGE_UNIT}", ""),
        ("", "/etc/systemd/user/x.service"),
    ):
        if path:
            probe.files[path] = "# my own unit\n"
        probe.outputs[unit_fragment(templates.BRIDGE_UNIT)] = f"{loaded}\n"
        with pytest.raises(PlanError, match="stops nothing and changes nothing"):
            build("debian", probe=probe, action=uninstall_plan)
    probe.files.clear()
    probe.outputs.clear()
    steps = build("debian", probe=probe, action=uninstall_plan)
    assert not any("disable" in argv for argv in commands(steps))


def test_linux_install_refuses_a_same_named_unit_loaded_from_elsewhere():
    probe = simulated_probe(simulated("arch"))
    probe.outputs[unit_fragment(templates.MCP_UNIT)] = "/etc/systemd/user/proton-mail-mcp.service"
    with pytest.raises(PlanError, match="does not shadow or restart"):
        build("arch", probe=probe)
    assert build("arch", probe=probe, skip_services=True)


def launchd_probe(path, plist_text=None):
    probe = simulated_probe(simulated("macos"))
    paths = paths_for(simulated("macos"), probe.env, probe.home)
    plist = f"{paths.agents_dir}/com.proton-mail-mcp.plist"
    probe.outputs[("launchctl", "print", "gui/501/com.proton-mail-mcp")] = (
        f"gui/501/com.proton-mail-mcp = {{\n\tpath = {path or plist}\n\tstdout path = /x\n}}\n"
    )
    if plist_text is not None:
        probe.files[plist] = plist_text
    return probe


def unloads(steps):
    return [argv for argv in commands(steps) if "bootout" in argv]


def test_macos_uninstall_unloads_only_the_installer_agents():
    owned = build("macos", probe=launchd_probe(None, f"<!-- {MARKER} -->"), action=uninstall_plan)
    assert unloads(owned) == [("launchctl", "bootout", "gui/501/com.proton-mail-mcp")]
    checks = [step for step in owned if isinstance(step, CheckStopped)]
    assert [step.argv for step in checks] == [("launchctl", "print", "gui/501/com.proton-mail-mcp")]
    assert [step.status for step in checks] == [planner.LAUNCHD_NOT_FOUND]
    for probe in (
        launchd_probe(None, "<plist>hand-written, with a token</plist>"),
        launchd_probe(None),
        launchd_probe("/Library/LaunchAgents/com.proton-mail-mcp.plist", f"<!-- {MARKER} -->"),
    ):
        with pytest.raises(PlanError) as refused:
            build("macos", probe=probe, action=uninstall_plan)
        assert str(refused.value).startswith(
            "com.proton-mail-mcp runs, or may run, from a definition the installer did not write"
        )
        assert "so the installer stops nothing and changes nothing" in str(refused.value)
        assert "token" not in str(refused.value)


def test_macos_install_refuses_an_agent_loaded_from_another_definition():
    for probe in (
        launchd_probe(None),
        launchd_probe("/Library/LaunchAgents/com.proton-mail-mcp.plist", f"<!-- {MARKER} -->"),
    ):
        with pytest.raises(PlanError, match="launchctl bootout gui/501/com.proton-mail-mcp"):
            build("macos", probe=probe)
    # A hand-written plist at the installer's path stops at WriteFile unless --force.
    steps = build("macos", probe=launchd_probe(None, "<plist>manual</plist>"))
    assert any(
        isinstance(s, WriteFile) and s.path.endswith("com.proton-mail-mcp.plist") for s in steps
    )


def test_windows_tasks_are_ended_or_deleted_only_when_the_installer_made_them():
    probe = simulated_probe(simulated("windows"))
    probe.outputs[task_query(BRIDGE_TASK)] = f"<Task><Description>{MARKER}.</Description></Task>"
    steps = build("windows", probe=probe, action=uninstall_plan)
    touched = [argv for argv in commands(steps) if argv[0] == "schtasks"]
    assert touched == [
        ("schtasks", "/End", "/TN", BRIDGE_TASK),
        ("schtasks", "/Delete", "/TN", BRIDGE_TASK, "/F"),
    ]
    (check,) = [step for step in steps if isinstance(step, CheckStopped)]
    assert "'proton-mail-mcp-bridge-HOST-you'" in check.argv[-1] and not check.states
    assert check.status == planner.TASK_NOT_RUNNING
    end, delete = (
        index(steps, lambda s, a=argv: isinstance(s, Command) and s.argv == a) for argv in touched
    )
    assert end < steps.index(check) < delete
    probe.outputs[task_query(MCP_TASK)] = "<Task><Description>someone else's</Description></Task>"
    for action in (uninstall_plan, install_plan):
        with pytest.raises(PlanError, match="never stopped or replaced"):
            build("windows", probe=probe, action=action)


# --skip-services never stops the Windows MCP task (Sol).


def test_windows_skip_services_never_queues_a_task_stop():
    probe = simulated_probe(simulated("windows"))
    probe.outputs[task_query(MCP_TASK)] = f"<Task><Description>{MARKER}.</Description></Task>"
    steps = build("windows", probe=probe, skip_services=True)
    assert not any(argv[0] == "schtasks" for argv in commands(steps))
    probe.succeeding.add(
        (
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "if ((Get-ScheduledTask -TaskPath '\\' -TaskName 'proton-mail-mcp-HOST-you' "
            "-ErrorAction Stop).State -eq 'Running') { exit 0 }; exit 3",
        )
    )
    with pytest.raises(PlanError) as caught:
        build("windows", probe=probe, skip_services=True)
    assert f'schtasks /End /TN "{MCP_TASK}"' in str(caught.value)
    assert "without --skip-services" in str(caught.value)
    stop = build("windows", probe=probe)
    assert ("schtasks", "/End", "/TN", MCP_TASK) in commands(stop)


# Bridge maintenance leaves the Linux MCP unit running (Sol).


def test_stopping_bridge_for_a_cli_login_leaves_the_mcp_unit_running():
    steps = build("fedora")
    unit = written(steps, templates.MCP_UNIT).content
    assert "Wants=proton-mail-mcp-bridge.service" in unit
    assert "After=proton-mail-mcp-bridge.service" in unit
    # These would stop (or restart) the MCP unit with Bridge, and a start would not undo it.
    for propagating in ("Requires=", "BindsTo=", "PartOf=", "Requisite="):
        assert propagating not in unit
    text = notes(steps)
    assert f"systemctl --user stop {templates.BRIDGE_UNIT}" in text
    assert f"systemctl --user start {templates.BRIDGE_UNIT}" in text
    assert "MCP service keeps running" in text


# State locations reach the service, not only the installing shell (Sol).


def state_build(family, env, existing=None):
    platform = simulated(family)
    probe = simulated_probe(platform)
    probe.env.update(env)
    paths = paths_for(platform, probe.env, probe.home)
    if existing is not None:
        probe.files[paths.env_file] = existing
    return build(family, probe=probe)


@pytest.mark.parametrize("family", SUPPORTED)
def test_resolved_state_locations_are_pinned_for_the_service(family):
    steps = state_build(family, {"XDG_STATE_HOME": "/data/state"})
    settings = env_file(steps)
    assert settings["PROTON_MCP_ACTION_LOG"] == "/data/state/proton-mail-mcp/actions.jsonl"
    assert settings["PROTON_MCP_IDENTITY_INDEX_DIR"] == "/data/state/proton-mail-mcp/identity-index"
    assert "/data/state/proton-mail-mcp/actions.jsonl" in notes(steps)
    # The server reads exactly these keys.
    config = Settings.from_env({**settings, "PROTON_MCP_TOKEN": "t" * 32})
    assert config.action_log_path == settings["PROTON_MCP_ACTION_LOG"]
    assert config.identity_index_dir == settings["PROTON_MCP_IDENTITY_INDEX_DIR"]
    default = env_file(state_build(family, {}))
    home = "/Users/you" if family == "macos" else "/home/you"
    assert default["PROTON_MCP_ACTION_LOG"] == f"{home}/.local/state/proton-mail-mcp/actions.jsonl"


def test_explicit_state_settings_win_and_are_kept_as_given():
    explicit = {
        "PROTON_MCP_ACTION_LOG": "/srv/audit/actions.jsonl",
        "PROTON_MCP_IDENTITY_INDEX_DIR": "/srv/index",
        "XDG_STATE_HOME": "/data/state",
    }
    steps = state_build("arch", explicit)
    settings = env_file(steps)
    assert settings["PROTON_MCP_ACTION_LOG"] == "/srv/audit/actions.jsonl"
    assert settings["PROTON_MCP_IDENTITY_INDEX_DIR"] == "/srv/index"
    content = written(steps, ".env").content
    # A later run from a plain shell keeps them, and the file does not change.
    rerun = state_build("arch", {}, existing=content)
    assert written(rerun, ".env").content == content
    # Values set in the env file by hand are kept too; the shell can still replace them.
    edited = content.replace("/srv/index", "/mnt/index")
    assert env_file(state_build("arch", {}, existing=edited))["PROTON_MCP_IDENTITY_INDEX_DIR"] == (
        "/mnt/index"
    )
    moved = state_build("arch", {"PROTON_MCP_IDENTITY_INDEX_DIR": "/new/index"}, existing=edited)
    assert env_file(moved)["PROTON_MCP_IDENTITY_INDEX_DIR"] == "/new/index"
    assert "changes from /mnt/index to /new/index" in notes(moved)
    with pytest.raises(PlanError, match="PROTON_MCP_ACTION_LOG must be an absolute path"):
        state_build("arch", {"PROTON_MCP_ACTION_LOG": "logs/actions.jsonl"})


def test_uninstall_names_the_state_it_keeps():
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = f"# {MARKER}\nPROTON_MCP_ACTION_LOG=/srv/audit/actions.jsonl\n"
    steps = build("arch", probe=probe, action=uninstall_plan)
    assert "/srv/audit/actions.jsonl" in notes(steps)
    assert f"{paths.state_dir}/identity-index" in notes(steps)


def test_linux_with_systemd_out_of_reach_is_refused_before_any_change():
    # Sol: on a systemd host the venv and settings changed before the service setup
    # failed. systemd runs this host, so services may be running unseen.
    platform = Platform("arch", "arch", "", "x86_64", "Arch Linux")
    probe = simulated_probe(platform)
    del probe.tools["systemctl"]
    with pytest.raises(PlanError) as refused:
        build("arch", probe=probe, platform=platform)
    message = str(refused.value)
    assert "systemctl is not on PATH, although systemd runs this host" in message
    assert "rerun with --skip-services" in message
    service = "/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp-service"
    assert f"`{service} --env-file /home/you/.config/proton-mail-mcp/proton-mail-mcp.env`" in (
        message
    )
    steps = build("arch", probe=probe, platform=platform, skip_services=True)
    assert not any(argv[0] == "systemctl" for argv in commands(steps))
    probe.tools["systemctl"] = "/usr/bin/systemctl"
    del probe.outputs[MANAGER_ENVIRONMENT]
    with pytest.raises(PlanError, match="cannot reach your systemd user manager"):
        build("arch", probe=probe, platform=platform)


@pytest.mark.parametrize(
    ("family", "markers", "env", "systemctl", "why"),
    [
        # The CI containers: systemctl may be installed, but systemd is not running.
        ("fedora", (), {}, False, "this host does not run systemd"),
        ("arch", ("/.dockerenv",), {}, True, "this host is a container (docker)"),
        ("fedora", ("/run/.containerenv",), {}, True, "this host is a container (podman)"),
        ("debian", (), {"container": "lxc"}, False, "this host is a container (lxc)"),
    ],
    ids=["no-systemd", "docker", "podman", "container-variable"],
)
def test_without_systemd_the_plan_degrades_to_no_services_and_says_why(
    family, markers, env, systemctl, why
):
    # Round 6 (c): a container or a host without systemd still gets a full plan,
    # and preflight says why no service is set up; nothing can be running there.
    platform = simulated(family)
    probe = simulated_probe(platform)
    del probe.files[planner.SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    probe.files.update({marker: "" for marker in markers})
    probe.env.update(env)
    if not systemctl:
        del probe.tools["systemctl"]
    steps = build(family, probe=probe, platform=platform)
    assert not any(argv[0] == "systemctl" for argv in commands(steps))
    assert not [step for step in steps if isinstance(step, WriteFile) and ".service" in step.path]
    text = notes(steps)
    assert f"Services: {why}" in text and "as with --skip-services" in text
    assert "proton-mail-mcp-service --env-file" in text
    # A moved venv stays, with no removal command: a service of the user's own may
    # still run from it.
    probe.files[env_file_path(platform, probe)] = written(
        build(family, probe=probe, platform=platform, venv=OLD), ".env"
    ).content
    probe.files.update({OLD: "", f"{OLD}/{DIR_MARKER}": MARKER})
    moved = build(family, probe=probe, platform=platform, venv=NEW)
    assert not directory_removals(moved)
    text = kept_note(moved, OLD).text
    assert f"stays: with --skip-services (or without systemd) no service was switched to {NEW}" in (
        text
    )
    assert "rm -rf" not in text


def env_file_path(platform, probe):
    return paths_for(platform, probe.env, probe.home).env_file


@pytest.mark.parametrize(
    ("booted", "systemctl", "manager", "why"),
    [
        (True, False, True, "systemctl is not on PATH, although systemd runs this host"),
        (True, True, False, "`systemctl --user` cannot reach your systemd user manager"),
        (False, False, False, "this host does not run systemd"),
    ],
    ids=["no-systemctl-on-path", "no-manager", "no-systemd"],
)
def test_linux_uninstall_keeps_installer_units_it_cannot_control(booted, systemctl, manager, why):
    # Sol: a restricted PATH proves nothing about running units, yet uninstall removed
    # them and the venv. Round 6 (b): nothing changes, and the manual steps are given.
    platform = simulated("arch")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[f"{paths.unit_dir}/{templates.MCP_UNIT}"] = f"# {MARKER}\n"
    if not booted:
        del probe.files[planner.SYSTEMD_BOOTED]
    if not systemctl:
        del probe.tools["systemctl"]
    if not manager:
        del probe.outputs[MANAGER_ENVIRONMENT]
    with pytest.raises(PlanError) as refused:
        build("arch", probe=probe, action=uninstall_plan)
    message = str(refused.value)
    assert f"{templates.MCP_UNIT} was written by the installer, but {why}" in message
    assert "Nothing was changed: the units, the virtual environment, the settings" in message
    assert f"`systemctl --user disable --now {templates.MCP_UNIT}`" in message
    assert f"`rm -f -- {paths.unit_dir}/{templates.MCP_UNIT}`" in message
    assert "Rerun uninstall afterwards" in message
    # Without units of the installer's, only files go (a container has none).
    del probe.files[f"{paths.unit_dir}/{templates.MCP_UNIT}"]
    steps = build("arch", probe=probe, action=uninstall_plan)
    assert not any(argv[0] == "systemctl" for argv in commands(steps))
    assert "no unit the installer wrote is here, so only files are removed" in notes(steps)


# Round 7 rules for every family: stops proven before any removal (R3), foreign MCP
# definitions keep the settings (R4), no definition content in any output (R5), only
# marked files and empty directories removed (R1, R2), opt-ins unchanged (R7).

SECRET = "smoke-hunter2-s3cr3t"


@pytest.mark.parametrize(
    ("family", "case"),
    [
        ("debian", "skip-services"),
        ("arch", "no-systemd"),
        ("fedora", "bridge-only"),
        ("macos", "no-agent"),
        ("windows", "no-task"),
    ],
)
def test_r7_rule4_the_settings_stay_until_the_installers_own_mcp_service_is_proven_stopped(
    family, case
):
    # Without an MCP service of the installer's to stop and check (installed with
    # --skip-services, or a host without systemd, where the installer says to run the
    # service yourself), nothing proves the settings and the venv unused: both stay,
    # each with the command that removes it by hand once nothing uses it.
    platform = simulated(family)
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    separator = "\\" if family == "windows" else "/"
    probe.files.update(
        {
            paths.env_file: f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\n",
            paths.venv: "",
            f"{paths.venv}{separator}{DIR_MARKER}": MARKER,
        }
    )
    if case == "no-systemd":
        del probe.files[planner.SYSTEMD_BOOTED]
        del probe.outputs[MANAGER_ENVIRONMENT]
    elif case == "bridge-only":
        probe.files[f"{paths.unit_dir}/{templates.BRIDGE_UNIT}"] = f"# {MARKER}\n"
    steps = build(family, probe=probe, action=uninstall_plan)
    assert paths.env_file not in [step.path for step in steps if isinstance(step, RemovePath)]
    stopped = "the installer stopped no MCP service of its own, so one of yours may"
    command = (
        f"Remove-Item -LiteralPath '{paths.env_file}' -Force"
        if family == "windows"
        else f"rm -f -- {shlex.quote(paths.env_file)}"
    )
    text = notes(steps)
    assert (
        f"The settings file {paths.env_file} stays: {stopped} read it. Once nothing does, "
        f"remove it with: `{command}`"
    ) in text
    assert "; the settings file stays." in text
    venv = kept_note(steps, paths.venv).text
    assert f"stays: {stopped} still run from it." in venv
    assert "rm -rf" not in venv and "Remove-Item" not in venv
    if case == "bridge-only":
        # The Bridge unit, which the installer wrote, is still stopped, checked and removed.
        (check,) = [step for step in steps if isinstance(step, CheckStopped)]
        assert check.argv[-1] == templates.BRIDGE_UNIT


def owned_services(family, probe, paths):
    """Every service of the installer's in place, loaded where that applies."""
    if family == "macos":
        for label in (templates.MCP_LABEL, templates.BRIDGE_LABEL):
            plist = f"{paths.agents_dir}/{label}.plist"
            probe.files[plist] = f"<!-- {MARKER} -->"
            probe.outputs[("launchctl", "print", f"gui/{probe.user_id}/{label}")] = (
                f"    path = {plist}\n"
            )
    elif family == "windows":
        for name in (MCP_TASK, BRIDGE_TASK):
            probe.outputs[task_query(name)] = f"<Task><Description>{MARKER}.</Description></Task>"
    else:
        for unit in (templates.MCP_UNIT, templates.BRIDGE_UNIT):
            probe.files[f"{paths.unit_dir}/{unit}"] = f"# {MARKER}\n"


def is_removal(step):
    """Anything that removes a file, a secret, a task or a package."""
    if isinstance(step, (RemovePath, DeleteSecret)):
        return True
    return isinstance(step, Command) and bool(
        {"/Delete", "remove", "uninstall", "-R"} & set(step.argv)
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_r7_rule3_every_stop_is_proven_before_anything_is_removed(family):
    # Sol r6 #4: uninstall went on after a stop failed. Each stop is now followed by a
    # check of its own, and every check comes before the first removal of any kind.
    platform = simulated(family)
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    owned_services(family, probe, paths)
    steps = build(
        family, probe=probe, action=uninstall_plan, remove_credentials=True, remove_bridge=True
    )
    checks = [number for number, step in enumerate(steps) if isinstance(step, CheckStopped)]
    stops = [
        number
        for number, step in enumerate(steps)
        if isinstance(step, Command) and {"disable", "bootout", "/End"} & set(step.argv)
    ]
    removals = [number for number, step in enumerate(steps) if is_removal(step)]
    assert len(checks) == 2 and stops and removals
    assert max(checks) < min(removals)
    assert all(any(stop < check for check in checks) for stop in stops)
    found = [steps[number] for number in checks]
    if family == "macos":
        assert [step.argv for step in found] == [
            ("launchctl", "print", f"gui/501/{label}")
            for label in (templates.MCP_LABEL, templates.BRIDGE_LABEL)
        ]
        assert all(not step.states and "launchctl bootout gui/501/" in step.hint for step in found)
        assert all(step.status == planner.LAUNCHD_NOT_FOUND for step in found)
    elif family == "windows":
        assert [step.argv[-1].split("'")[3] for step in found] == [
            MCP_TASK.lstrip("\\"),
            BRIDGE_TASK.lstrip("\\"),
        ]
        assert all(not step.states and "schtasks /End /TN" in step.hint for step in found)
        assert all(step.status == planner.TASK_NOT_RUNNING for step in found)
    else:
        assert [step.argv for step in found] == [
            ("systemctl", "--user", "show", "--property=ActiveState", "--value", unit)
            for unit in (templates.MCP_UNIT, templates.BRIDGE_UNIT)
        ]
        assert all(step.states == ("inactive", "failed") for step in found)
        assert all(
            step.hint.startswith(
                "Nothing was removed. Stop it yourself with `systemctl --user disable --now "
                f"{templates.MCP_UNIT} {templates.BRIDGE_UNIT}` (a unit that refuses manual "
                "stops needs that setting removed;"
            )
            for step in found
        )
    # The stop commands may fail; what they did is checked instead.
    assert all(steps[number].allow_failure for number in stops)


def test_r7_rule3_an_agent_not_loaded_while_planning_is_still_checked():
    platform = simulated("macos")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[f"{paths.agents_dir}/{templates.MCP_LABEL}.plist"] = f"<!-- {MARKER} -->"
    steps = build("macos", probe=probe, action=uninstall_plan)
    assert not unloads(steps)
    (check,) = [step for step in steps if isinstance(step, CheckStopped)]
    assert check.argv == ("launchctl", "print", f"gui/501/{templates.MCP_LABEL}")
    assert steps.index(check) < min(number for number, s in enumerate(steps) if is_removal(s))


@pytest.mark.parametrize(
    ("family", "case"),
    [
        ("debian", "loaded-elsewhere"),
        ("fedora", "unmarked-file"),
        ("arch", "fragment-unknown"),
        ("debian", "bridge-fragment-unknown"),
        ("macos", "loaded-elsewhere"),
        ("macos", "unmarked-file"),
        ("macos", "path-unknown"),
        ("windows", "foreign-task"),
    ],
)
def test_r8_a_definition_the_installer_cannot_vouch_for_stops_uninstall(family, case):
    # Sol r7 #4: a failed FragmentPath query made a marked local file look like the
    # installer's unit, so uninstall could stop a foreign definition loaded under its
    # name. A definition the installer did not write, or one the service manager does
    # not name, under any of its service names now stops uninstall before anything is
    # stopped or removed; the message gives the manual steps and no definition content.
    platform = simulated(family)
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\n"
    owned_services(family, probe, paths)
    expected = "stops nothing and changes nothing"
    if family == "macos":
        name = templates.MCP_LABEL
        plist = f"{paths.agents_dir}/{name}.plist"
        loaded = {
            "loaded-elsewhere": f"    path = /Library/LaunchAgents/{name}.plist\n",
            "unmarked-file": f"    path = {plist}\n",
            "path-unknown": "    state = running\n",
        }[case]
        probe.outputs[("launchctl", "print", f"gui/501/{name}")] = loaded
        if case == "unmarked-file":
            probe.files[plist] = f"<plist>hand-written, token {SECRET}</plist>"
        check = f"`launchctl print gui/501/{name}` shows that definition"
    elif family == "windows":
        probe.outputs[task_query(MCP_TASK)] = f"<Task><Description>{SECRET}</Description></Task>"
        expected = check = f"A task named {MCP_TASK} exists that the installer did not create"
    else:
        name = templates.MCP_UNIT if case != "bridge-fragment-unknown" else templates.BRIDGE_UNIT
        check = f"`systemctl --user status {name}`"
        if case == "loaded-elsewhere":
            probe.outputs[unit_fragment(name)] = f"/etc/systemd/user/{name}\n"
        elif case == "unmarked-file":
            probe.files[f"{paths.unit_dir}/{name}"] = f"[Service]\nExecStart=/bin/mine {SECRET}\n"
        else:
            # systemd runs, but does not answer which file it loads for the name.
            del probe.outputs[unit_fragment(name)]
            # Install stops before anything changes too: it would restart that unit.
            with pytest.raises(PlanError, match=expected):
                build(family, probe=probe)
    with pytest.raises(PlanError) as refused:
        build(family, probe=probe, action=uninstall_plan)
    message = str(refused.value)
    assert expected in message and check in message and SECRET not in message


def seed_foreign_definitions(family, probe, paths):
    """Definitions and environments the installer did not write, each with a secret."""
    if family == "macos":
        probe.files[f"{paths.agents_dir}/com.example.tool.plist"] = plistlib.dumps(
            {"Label": "com.example.tool", "ProgramArguments": [f"{OLD}/bin/tool", SECRET]}
        ).decode()
        return
    units = paths.unit_dir
    probe.links.update({"/srv/loop/a": "/srv/loop/b", "/srv/loop/b": "/srv/loop/a"})
    probe.files.update(
        {
            f"{units}/tool.service": (
                f'[Service]\nExecStart="/srv/loop/a/tool --password {SECRET}"\n'
                "EnvironmentFile=%h/.config/tool.env\n"
            ),
            f"{units}/tool.service.d/10-env.conf": (
                f'[Service]\nEnvironment="TOOL_DB=postgres://tool:{SECRET}@localhost/db"\n'
            ),
            "/home/you/.config/tool.env": f"TOOL_HOME={OLD}\nTOOL_PASSWORD={SECRET}\n",
        }
    )
    probe.outputs[MANAGER_ENVIRONMENT] = f"HOME=/home/you\nTOOL_TOKEN={SECRET}\n"


def visible(steps):
    """Every field of every step and every line a dry run prints."""
    out = io.StringIO()
    Executor(dry_run=True, out=out, windows=False).run(steps)
    return out.getvalue() + repr([dataclasses.asdict(step) for step in steps])


@pytest.mark.parametrize("family", SUPPORTED)
def test_r7_rule5_no_content_of_a_definition_the_installer_did_not_write_reaches_output(family):
    # Astra r6 #2: values the unit parser could not resolve reached the report raw. The
    # planner reads no such definition now, so nothing of one can reach a step, a
    # printed line, a PlanError or installer.log (logged notes are notes here).
    probe, paths = moved_venv_probe(family)
    seed_foreign_definitions(family, probe, paths)
    owned_services(family, probe, paths)
    moved = build(family, probe=probe, venv=NEW, skip_bridge_service=True)
    removed = build(family, probe=probe, action=uninstall_plan)
    for steps in (moved, removed):
        assert SECRET not in visible(steps)
    assert kept_note(moved, OLD) and kept_note(removed, OLD)
    if family != "macos":
        # While the user manager is out of reach, uninstall refuses; the refusal names
        # units and files only.
        del probe.outputs[MANAGER_ENVIRONMENT]
        with pytest.raises(PlanError) as refused:
            build(family, probe=probe, action=uninstall_plan)
        assert SECRET not in str(refused.value) and "Nothing was changed" in str(refused.value)


def family_venvs(family):
    return ("C:\\pmm\\old", "C:\\pmm\\new") if family == "windows" else (OLD, NEW)


def allowed_removals(paths):
    """The single files an installer plan may remove, and the directories it may rmdir."""
    files = {paths.env_file}
    if paths.unit_dir:
        files |= {
            f"{paths.unit_dir}/{unit}" for unit in (templates.MCP_UNIT, templates.BRIDGE_UNIT)
        }
    if paths.agents_dir:
        labels = (templates.MCP_LABEL, templates.BRIDGE_LABEL)
        files |= {f"{paths.agents_dir}/{label}.plist" for label in labels}
    if paths.task_dir:
        files |= {f"{paths.task_dir}\\{name}" for name in ("mcp-task.xml", "bridge-task.xml")}
    directories = {paths.config_dir, paths.data_dir, *([paths.task_dir] if paths.task_dir else [])}
    return files, directories


@pytest.mark.parametrize(
    "scenario",
    [
        "install",
        "install-venv-move",
        "install-skip-services",
        "install-previous-list",
        "uninstall",
        "uninstall-remove-credentials",
    ],
)
@pytest.mark.parametrize("family", FAMILIES)
def test_r7_every_removal_is_a_marked_file_or_an_empty_directory(family, scenario):
    # R1 and R2 in every plan: a RemovePath names a service file, the settings file or
    # the package this run downloaded; directory=True only an installer directory,
    # which goes only once empty. Nothing removes a venv or the downloads.
    platform = simulated(family)
    old, new = family_venvs(family)
    probe, paths = moved_venv_probe(family, old=old)
    options_for = {"venv": new}
    if scenario == "install":
        options_for = {}
    elif scenario == "install-skip-services":
        options_for["skip_services"] = True
    elif scenario == "install-previous-list":
        probe.files[paths.env_file] = written(build(family, probe=probe, venv=new), ".env").content
        options_for = {}
    else:
        owned_services(family, probe, paths)
    if scenario.startswith("uninstall"):
        flags = {"remove_credentials": True, "remove_bridge": True}
        options_for = flags if scenario.endswith("credentials") else {}
        steps = build(family, probe=probe, action=uninstall_plan, **options_for)
    else:
        steps = build(family, probe=probe, **options_for)
    planned = paths_for(platform, probe.env, probe.home)
    files, directories = allowed_removals(planned)
    for step in steps:
        if not isinstance(step, RemovePath):
            continue
        assert old not in step.path and new not in step.path
        if step.directory:
            assert step.path in directories, step
        elif not step.managed_only:
            # Only the package this run downloaded and verified.
            assert step.path.startswith(f"{planned.cache_dir}/") and step.path.endswith(
                (".deb", ".rpm")
            ), step
        else:
            assert step.path in files, step


def test_r8_printed_token_commands_quote_the_account():
    # Astra r7 #6: accounts may hold $, ; or (), which ran as shell code when pasted.
    account = "me$(id);x@example.com"
    for store, word in (
        ("keychain", account),
        ("libsecret", account),
        ("pass", f"{TOKEN_SERVICE}/{account}"),
    ):
        command = planner.token_hint(store, account).split("`")[1]
        assert shlex.quote(word) in command and word in shlex.split(command)


@pytest.mark.parametrize(
    ("family", "check", "not_found", "failure"),
    [
        ("macos", "find-generic-password", 44, subprocess.TimeoutExpired("security", 10)),
        (
            "debian",
            "lookup",
            0,
            subprocess.CompletedProcess([], 1, "", "secret-tool: Cannot autolaunch D-Bus\n"),
        ),
    ],
)
def test_r9_a_failed_credential_check_keeps_what_is_stored_and_stops(
    monkeypatch, family, check, not_found, failure
):
    # Astra r8 #2: a Keychain check that timed out, or a failed secret-tool lookup,
    # counted as "no token", so the plan generated one over the token clients use.
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if TOKEN_SERVICE not in argv:
            # No Bridge password: Keychain's "not found", or an empty libsecret entry.
            return subprocess.CompletedProcess(argv, not_found, "", "")
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(subprocess, "run", run)
    platform = simulated(family)
    probe = simulated_probe(platform)
    probe.has_secret = HostProbe(platform).has_secret
    with pytest.raises(PlanError) as caught:
        build(family, probe=probe)
    message = str(caught.value)
    assert message.startswith("Cannot tell whether the MCP bearer token is already in ")
    assert "what is stored stays" in message and "--skip-credentials" in message
    # Only the two checks ran: nothing was stored and no token was generated.
    assert [argv[1] for argv in calls] == [check, check]


def test_r9_printed_launch_commands_quote_their_paths():
    # Sol r8 #2: with --venv '/srv/My PMM' the pasted service command ran /srv/My.
    platform = simulated("debian")
    probe = simulated_probe(platform)
    probe.env["XDG_CONFIG_HOME"] = "/home/you/my config"
    # Without systemd the plan names the command to run at login.
    del probe.files[planner.SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    steps = build("debian", probe=probe, venv="/srv/My PMM", bridge_bin="/opt/My Bridge/bridge")
    pasted = [shlex.split(command) for command in notes(steps).split("`")[1::2]]
    settings = "/home/you/my config/proton-mail-mcp/proton-mail-mcp.env"
    assert ["/srv/My PMM/bin/proton-mail-mcp-service", "--env-file", settings] in pasted
    assert ["/opt/My Bridge/bridge", "--cli"] in pasted


def test_r10_a_silent_secret_tool_lookup_keeps_the_token_and_stops(monkeypatch):
    # Astra r9 #1: secret-tool exits 1 and prints nothing for no entry, but also for a
    # locked one whose unlock prompt was dismissed. Read as "no token", the plan
    # generated one over the token clients use.
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if TOKEN_SERVICE in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "synthetic-bridge-password\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    platform = simulated("debian")
    probe = simulated_probe(platform)
    probe.has_secret = HostProbe(platform).has_secret
    with pytest.raises(PlanError) as caught:
        build("debian", probe=probe)
    message = str(caught.value)
    assert message.startswith("Cannot tell whether the MCP bearer token is already in libsecret")
    assert "what is stored stays" in message
    assert "If none is stored yet, rerun with --replace-credentials" in message
    # Only the two lookups ran: nothing was stored and no token was generated.
    assert [argv[:2] for argv in calls] == [["secret-tool", "lookup"]] * 2
    # --replace-credentials is the only way to store a new token: nothing is looked up.
    calls.clear()
    steps = build("debian", probe=probe, replace_credentials=True)
    assert calls == []
    assert [(step.service, step.source) for step in steps if isinstance(step, StoreSecret)] == [
        (BRIDGE_SERVICE, "prompt"),
        (TOKEN_SERVICE, "generate"),
    ]


def test_r10_macos_printed_login_commands_quote_their_paths():
    # Sol and Astra r9 #2: the macOS hints put paths in double quotes, so a pasted
    # --bridge-bin '/opt/Bridge$HOME/bridge' expanded $HOME.
    platform = simulated("macos")
    probe = simulated_probe(platform)
    probe.home = "/Users/$you"
    exe = "/opt/Bridge$HOME/bridge"
    plist = f"/Users/$you/Library/LaunchAgents/{templates.BRIDGE_LABEL}.plist"
    text = notes(build("macos", probe=probe, bridge_bin=exe))
    assert f"`{shlex.join([exe, '--cli'])}`" in text
    assert f"`{shlex.join(['launchctl', 'bootstrap', 'gui/501', plist])}`" in text


def test_r7_rule7_opt_in_removals_are_unchanged_and_come_after_the_checks():
    # R7: --remove-credentials and --remove-bridge work as before, after every check.
    platform = simulated("fedora")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    owned_services("fedora", probe, paths)
    steps = build(
        "fedora", probe=probe, action=uninstall_plan, remove_credentials=True, remove_bridge=True
    )
    deleted = [(step.service, step.account) for step in steps if isinstance(step, DeleteSecret)]
    assert deleted == [(BRIDGE_SERVICE, USER), (TOKEN_SERVICE, USER)]
    package = index(steps, lambda s: isinstance(s, Command) and s.argv[:2] == ("dnf", "remove"))
    last_check = max(number for number, s in enumerate(steps) if isinstance(s, CheckStopped))
    first_secret = index(steps, lambda s: isinstance(s, DeleteSecret))
    assert last_check < first_secret < package


# Structural guards: the class of finding cannot come back unnoticed.

INSTALLER = Path(planner.__file__).parent


def test_r7_installer_never_recursively_deletes_or_copies():
    # R1: no rmtree, copytree or removedirs anywhere in the installer, as an attribute,
    # a name or an import; nor TemporaryDirectory, whose cleanup is an rmtree (r7 #4).
    forbidden = {"rmtree", "copytree", "removedirs", "TemporaryDirectory"}
    sources = sorted(INSTALLER.glob("*.py"))
    assert {source.name for source in sources} >= {"plan.py", "execute.py", "probe.py"}
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (source.name, node.attr)
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden, (source.name, node.id)
            elif isinstance(node, ast.alias):
                assert node.name.rpartition(".")[2] not in forbidden, (source.name, node.name)


def test_r7_planner_parses_no_service_definitions():
    # R5: what the planner never parses cannot leak. plan.py imports no plist or XML
    # parser, and no probe lists directories or reads bytes.
    tree = ast.parse((INSTALLER / "plan.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name == "plistlib" or name.split(".")[0] == "xml"}
    for probe in (planner.Probe, HostProbe, FakeProbe):
        for name in ("listdir", "read_bytes", "links_under", "same_tree", "cwd"):
            assert not hasattr(probe, name), (probe.__name__, name)
