"""Service definitions and the non-secret env file; none of them hold a secret.

The systemd units and LaunchAgents are package data in service_templates, so an
installed wheel needs no checkout.
"""

import plistlib
import re
import string
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from importlib import resources

from ..service_entry import SECRET_KEYS, parse_env_file
from .steps import MARKER

BRIDGE_UNIT = "proton-mail-mcp-bridge.service"
MCP_UNIT = "proton-mail-mcp.service"
BRIDGE_LABEL = "com.proton-mail-mcp.bridge"
MCP_LABEL = "com.proton-mail-mcp"
# Root-folder task names: standard users may create tasks there, not new folders.
# The user suffix keeps tasks of different accounts on one machine apart.
BRIDGE_TASK_PREFIX = "proton-mail-mcp-bridge"
MCP_TASK_PREFIX = "proton-mail-mcp"
ENV_HEADER = (
    f"# {MARKER}.",
    "# Non-secret settings only: the Bridge password and the MCP bearer token stay in",
    "# the credential store named below. Lines the installer does not own are kept.",
)
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SYSTEMD_PLAIN = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


class TemplateError(ValueError):
    """A value cannot be rendered safely into a service definition."""


def task_name(prefix: str, user_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", user_id).strip("-.") or "user"
    return f"\\{prefix}-{slug}"


def _no_newlines(*values: str) -> None:
    for value in values:
        if any(char in value for char in "\r\n\0"):
            raise TemplateError("Paths and arguments must not contain line breaks")


def systemd_arg(value: str) -> str:
    """Quote one ExecStart argument; % and $ would otherwise be expanded."""
    _no_newlines(value)
    escaped = value.replace("%", "%%").replace("$", "$$")
    if _SYSTEMD_PLAIN.fullmatch(escaped):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _template(name: str) -> str:
    folder = resources.files(__package__) / "service_templates"
    return (folder / name).read_text(encoding="utf-8")


def _filled(value, values: Mapping[str, str]):
    """Fill ${NAME} in every string of a parsed plist; a value is never read as a template."""
    if isinstance(value, str):
        return string.Template(value).substitute(values)
    if isinstance(value, list):
        return [_filled(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _filled(item, values) for key, item in value.items()}
    return value


def systemd_bridge_unit(bridge_exe: str) -> str:
    return string.Template(_template(BRIDGE_UNIT)).substitute(
        MARKER=MARKER, BRIDGE_EXE=systemd_arg(bridge_exe)
    )


def systemd_mcp_unit(service_exe: str, env_file: str, port: int, bridge_unit: bool) -> str:
    # Wants=, not Requires=: stopping Bridge for a CLI login must not stop the MCP
    # server, which reconnects to Bridge on its next request.
    ordering = f"\nAfter={BRIDGE_UNIT}\nWants={BRIDGE_UNIT}" if bridge_unit else ""
    return string.Template(_template(MCP_UNIT)).substitute(
        MARKER=MARKER,
        PORT=port,
        BRIDGE_ORDERING=ordering,
        SERVICE_EXE=systemd_arg(service_exe),
        ENV_FILE=systemd_arg(env_file),
    )


def _plist(name: str, **values: str) -> str:
    # plistlib escapes the values for XML, which plain text substitution would not.
    payload = _filled(plistlib.loads(_template(name).encode("utf-8")), values)
    text = plistlib.dumps(payload, sort_keys=True).decode("utf-8")
    # plistlib cannot emit comments; the marker sits between the DOCTYPE and the root.
    return text.replace('<plist version="1.0">', f'<!-- {MARKER} -->\n<plist version="1.0">', 1)


def launchd_bridge_plist(bridge_exe: str, log_dir: str) -> str:
    _no_newlines(bridge_exe, log_dir)
    return _plist(
        f"{BRIDGE_LABEL}.plist", LABEL=BRIDGE_LABEL, BRIDGE_EXE=bridge_exe, LOG_DIR=log_dir
    )


def launchd_mcp_plist(service_exe: str, env_file: str, workdir: str, log_dir: str) -> str:
    _no_newlines(service_exe, env_file, workdir, log_dir)
    return _plist(
        f"{MCP_LABEL}.plist",
        LABEL=MCP_LABEL,
        SERVICE_EXE=service_exe,
        ENV_FILE=env_file,
        WORKDIR=workdir,
        LOG_DIR=log_dir,
    )


def scheduled_task_xml(
    *,
    description: str,
    user_id: str,
    command: str,
    arguments: Sequence[str],
    workdir: str,
    delay: str | None = None,
) -> str:
    """Logon task for the current user with an interactive token and no stored password.

    An interactive token keeps DPAPI, and so Windows Credential Manager, available
    to Bridge and the MCP server; tasks run only while the user is logged on.
    """
    _no_newlines(user_id, command, workdir, *arguments)
    ET.register_namespace("", _TASK_NS)

    def node(parent, tag, text=None):
        element = ET.SubElement(parent, f"{{{_TASK_NS}}}{tag}")
        if text is not None:
            element.text = text
        return element

    task = ET.Element(f"{{{_TASK_NS}}}Task", {"version": "1.2"})
    info = node(task, "RegistrationInfo")
    node(info, "Description", f"{MARKER}. {description}")
    trigger = node(node(task, "Triggers"), "LogonTrigger")
    node(trigger, "Enabled", "true")
    node(trigger, "UserId", user_id)
    if delay:
        node(trigger, "Delay", delay)
    principal = node(node(task, "Principals"), "Principal")
    principal.set("id", "Author")
    node(principal, "UserId", user_id)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for tag, text in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("AllowStartOnDemand", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("RunOnlyIfIdle", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Priority", "7"),
    ):
        node(settings, tag, text)
    restart = node(settings, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "999")
    actions = node(task, "Actions")
    actions.set("Context", "Author")
    action = node(actions, "Exec")
    node(action, "Command", command)
    if arguments:
        node(action, "Arguments", subprocess.list2cmdline(list(arguments)))
    node(action, "WorkingDirectory", workdir)
    ET.indent(task)
    body = ET.tostring(task, encoding="unicode")
    # schtasks /XML expects UTF-16; the executor writes this text with that encoding.
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + body + "\n"


def render_env_file(
    existing: str | None, owned: Mapping[str, str], defaults: Mapping[str, str]
) -> str:
    """Set installer-owned keys, add missing defaults, and keep every other line."""
    if existing:
        # Refuses secret keys and malformed lines; neither is copied or echoed.
        parse_env_file(existing)
    lines = list(ENV_HEADER)
    seen = set()
    for raw in (existing or "").splitlines():
        stripped = raw.strip()
        if stripped in ENV_HEADER:
            continue
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if stripped.startswith("#") or not key:
            lines.append(raw.rstrip())
            continue
        seen.add(key)
        lines.append(f"{key}={owned[key]}" if key in owned else raw.rstrip())
    for values in (owned, defaults):
        for key, value in values.items():
            if key in SECRET_KEYS:
                raise TemplateError(f"{key} must never be written to the env file")
            _no_newlines(value)
            if key not in seen:
                lines.append(f"{key}={value}")
                seen.add(key)
    return "\n".join(lines) + "\n"
