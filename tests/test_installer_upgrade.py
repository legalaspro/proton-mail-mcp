"""Moving the venv and uninstalling, end to end on real directories.

Each test plans with the real planner against files under tmp_path, then applies the
plan with the real executor. Only the programs it would start are scripted: pip,
systemctl, launchctl and the port check. Nothing touches the network or the user's
services. Since round 7 the installer never deletes a directory with content, so each
test checks that the old and the current venv, the downloads and the data stay byte
for byte, and that uninstall removes only the marked files it wrote, once every
service it stopped is proven stopped.
"""

import hashlib
import io
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from proton_mail_mcp.installer import templates
from proton_mail_mcp.installer.detect import simulated
from proton_mail_mcp.installer.execute import Executor, InstallError
from proton_mail_mcp.installer.plan import (
    MANAGER_ENVIRONMENT,
    PREVIOUS_VENVS_KEY,
    SYSTEMD_BOOTED,
    VENV_KEY,
    Options,
    Paths,
    PlanError,
    install_plan,
    paths_for,
    uninstall_plan,
)
from proton_mail_mcp.installer.probe import HostProbe
from proton_mail_mcp.installer.steps import DIR_MARKER, MARKER
from proton_mail_mcp.service_entry import parse_env_file

# The plans are Linux and macOS plans with POSIX paths; the installer refuses Windows.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="the plans use POSIX paths")

USER = "you@example.com"
SECRET = "smoke-hunter2-s3cr3t"
# Where each data setting points, relative to the directory holding the data.
SETTINGS = {
    "PROTON_MCP_ACTION_LOG": "audit/actions.jsonl",
    "PROTON_MCP_IDENTITY_INDEX_DIR": "identity-index",
    "PROTON_MCP_SAVED_RULES": "rules/custom.json",
    "PROTON_MCP_PROTECTED_CONFIG": "protected.json",
    "PROTON_BRIDGE_CERT": "bridge-cert.pem",
    "PROTON_MCP_PASSWORD_STORE_DIR": "pass",
    "PROTON_MCP_GNUPGHOME": "gnupg",
}
# The user's data: settings above, and what the server keeps beside the action log
# (rules timer state, default saved rules, search index).
DATA = {
    "audit/actions.jsonl": b'{"action": "move", "uids": [1, 2, 3]}\n' * 500,
    "audit/rules-state.json": b'{"last_uid": 41}',
    "audit/saved-rules.json": b'{"rules": []}',
    "audit/search-index/0123abcd.sqlite3": bytes(range(256)) * 64,
    "identity-index/scope/inventory.json": b'{"uidvalidity": 7}',
    "rules/custom.json": b'{"rules": [{"name": "news"}]}',
    "protected.json": b'{"senders": ["boss@example.com"]}',
    "bridge-cert.pem": b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
    "pass/.gpg-id": b"0123456789ABCDEF\n",
    "pass/proton-mail-mcp MCP bearer token/you@example.com.gpg": os.urandom(700),
    "pass/proton-mail-mcp Bridge password/you@example.com.gpg": os.urandom(650),
    "gnupg/pubring.kbx": os.urandom(2048),
    "gnupg/private-keys-v1.d/ABCDEF.key": os.urandom(1200),
}
FRAGMENT = ("systemctl", "--user", "show", "--property=FragmentPath", "--value")
ACTIVE_STATE = ("systemctl", "--user", "show", "--property=ActiveState", "--value")
STOP_ADVICE = (
    "not proven stopped. Nothing was removed. Stop it yourself with `systemctl --user "
    f"disable --now {templates.MCP_UNIT} {templates.BRIDGE_UNIT}` (a unit that refuses "
    "manual stops needs that setting removed;"
)


class DiskProbe(HostProbe):
    """The files under tmp_path as they are; tools and service managers are scripted.

    The scripted systemd user manager loads each unit from the user unit directory
    unless fragments names another file for it; its environment, which may hold
    anything, is never shown. The scripted launchd has the labels in launchd loaded.
    """

    def __init__(self, home: Path) -> None:
        self.env: dict[str, str] = {}
        self.home = str(home)
        self.user_id = "1000"
        self.unit_dir = home / ".config" / "systemd" / "user"
        self.systemd = True
        self.missing: set[str] = set()
        self.fragments: dict[str, str | None] = {}
        self.manager = f"HOME={home}\nLANG=C.UTF-8\n"
        self.launchd: dict[str, str] = {}

    def which(self, name: str) -> str | None:
        return None if name in self.missing else f"/usr/bin/{name}"

    def exists(self, path: str) -> bool:
        # Booted with systemd as scripted, also in a CI container that is not.
        return self.systemd if path == SYSTEMD_BOOTED else super().exists(path)

    def output(self, argv: tuple[str, ...]) -> str | None:
        argv = tuple(argv)
        if argv[:2] == ("launchctl", "print"):
            loaded = self.launchd.get(argv[2].rpartition("/")[2])
            return None if loaded is None else f"gui/1000 = {{\n\tpath = {loaded}\n}}\n"
        if not self.systemd:
            return None
        if argv == MANAGER_ENVIRONMENT:
            return self.manager
        if argv[:-1] == FRAGMENT:
            unit = argv[-1]
            fragment = self.unit_dir / unit
            # None: systemd does not say which file it loads.
            loaded = self.fragments.get(unit, str(fragment) if fragment.exists() else "")
            return None if loaded is None else loaded + "\n"
        return None

    def can_make_venv(self, python: str) -> bool:
        return True

    def port_open(self, port: int) -> bool:
        return False

    def has_secret(self, *args, **kwargs) -> bool:
        return True


class Runner:
    """Every program succeeds unless results scripts its exit status and output, by
    argv prefix. systemd reports each unit inactive and launchd no label loaded, as
    after a stop that worked."""

    def __init__(self, results=None):
        self.calls = []
        self.results = dict(results or {})

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        code, stdout = 0, ""
        if tuple(argv[:5]) == ACTIVE_STATE:
            stdout = "inactive\n"
        elif argv[:2] == ["launchctl", "print"]:
            code = 113
        for prefix, result in self.results.items():
            if tuple(argv[: len(prefix)]) == prefix:
                code, stdout = result
        return subprocess.CompletedProcess(argv, code, stdout, "")


@dataclass
class Host:
    root: Path
    old: Path
    new: Path
    bridge: Path
    paths: Paths
    probe: DiskProbe

    @property
    def unit_dir(self) -> Path:
        return Path(self.paths.unit_dir)

    def settings(self) -> dict[str, str]:
        return parse_env_file(Path(self.paths.env_file).read_text(encoding="utf-8"))


def snapshot(top: Path) -> dict[str, object]:
    """Relative path -> (size, SHA-256), link target or "dir"; links are not followed,
    sockets and pipes are left out."""
    found: dict[str, object] = {}
    pending = [top]
    while pending:
        path = pending.pop()
        name = path.relative_to(top).as_posix()
        if path.is_symlink():
            found[name] = ("link", os.readlink(path))
        elif path.is_dir():
            found[name] = "dir"
            pending.extend(path.iterdir())
        elif path.is_file():
            data = path.read_bytes()
            found[name] = (len(data), hashlib.sha256(data).hexdigest())
    return found


def write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_bytes(content)
    return path


def installed(tmp_path: Path, old: Path | None = None, data_in_venv: bool = True) -> Host:
    """A host installed with --venv old (default tmp_path/old-venv). The user's data
    lives in that venv, or with data_in_venv False in tmp_path/data."""
    home = tmp_path / "home"
    old = old or tmp_path / "old-venv"
    home.mkdir()
    write(old / DIR_MARKER, f"{MARKER}; the installer created it and never deletes it.\n")
    write(old / "bin" / "proton-mail-mcp-service", "#!/bin/sh\n")
    data = old if data_in_venv else tmp_path / "data"
    for name, content in DATA.items():
        write(data / name, content)
    # GnuPG's private layout, a relative link and gpg-agent's socket (here a pipe).
    (data / "gnupg").chmod(0o700)
    (data / "identity-index" / "latest").symlink_to("scope", target_is_directory=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(data / "gnupg" / "S.gpg-agent")
    bridge = write(tmp_path / "bridge" / "protonmail-bridge", "#!/bin/sh\n")
    probe = DiskProbe(home)
    paths = paths_for(simulated("debian"), probe.env, probe.home)
    settings = {
        "PROTON_BRIDGE_USER": USER,
        "PROTON_MCP_CREDENTIAL_STORE": "pass",
        VENV_KEY: str(old),
        **{key: str(data / name) for key, name in SETTINGS.items()},
    }
    write(
        Path(paths.env_file),
        templates.render_env_file(None, settings, {"PROTON_MCP_CONFIRM_MUTATIONS": "1"}),
    )
    service = str(old / "bin" / "proton-mail-mcp-service")
    unit_dir = Path(paths.unit_dir)
    write(
        unit_dir / templates.MCP_UNIT,
        templates.systemd_mcp_unit(service, paths.env_file, 8765, True),
    )
    write(unit_dir / templates.BRIDGE_UNIT, templates.systemd_bridge_unit(str(bridge)))
    return Host(tmp_path, old, tmp_path / "new-venv", bridge, paths, probe)


def downloads(host: Host) -> Path:
    """The download directory a Bridge install left: marked, with Proton's key."""
    cache = Path(host.paths.cache_dir)
    write(cache / DIR_MARKER, f"{MARKER}; the installer created it and never deletes it.\n")
    write(cache / "bridge_pubkey.gpg", os.urandom(1800))
    return cache


def plan(host: Host, action=install_plan, **overrides) -> list:
    """The plan for the files on disk."""
    values = {"bridge_user": USER, "python": sys.executable}
    if action is install_plan:
        values.update(
            source=str(host.root / "checkout"),
            venv=str(host.new),
            skip_bridge=True,
            skip_credentials=True,
            bridge_bin=str(host.bridge),
        )
    return action(simulated("debian"), Options(**{**values, **overrides}), host.probe, host.paths)


def execute(steps: list, runner=None) -> tuple[str, Runner, InstallError | None]:
    """Apply steps for real; the printed output, the runner, and the error if one."""
    out, runner = io.StringIO(), runner or Runner()
    executor = Executor(
        dry_run=False,
        out=out,
        runner=runner,
        port_open=lambda port: True,
        sleep=lambda seconds: None,
        windows=False,
    )
    try:
        executor.run(steps)
    except InstallError as error:
        return out.getvalue(), runner, error
    return out.getvalue(), runner, None


def apply(host: Host, action=install_plan, runner=None, **overrides) -> tuple[str, Runner]:
    """Plan against the files on disk, then apply the plan; it must succeed."""
    output, runner, error = execute(plan(host, action, **overrides), runner)
    assert error is None, f"{error}\n{output}"
    return output, runner


def installer_log(host: Host) -> str:
    return (Path(host.paths.state_dir) / "installer.log").read_text()


def assert_settings_follow(host: Host, data: Path) -> None:
    settings = host.settings()
    assert {key: settings[key] for key in SETTINGS} == {
        key: str(data / name) for key, name in SETTINGS.items()
    }


def test_r7_moving_the_venv_keeps_every_byte_of_the_old_one(tmp_path):
    # R1: the installer never deletes a directory with content. The data in the old
    # venv stays there, its settings still point there, and the note says so.
    host = installed(tmp_path)
    before = snapshot(host.old)
    output, runner = apply(host)
    assert snapshot(host.old) == before
    settings = host.settings()
    assert settings[VENV_KEY] == str(host.new)
    assert settings[PREVIOUS_VENVS_KEY] == f'["{host.old}"]'
    assert_settings_follow(host, host.old)
    for text in (output, installer_log(host)):
        assert f"The previous virtual environment {host.old} (" in text
        assert f"still holds what this installation uses: {', '.join(SETTINGS)}." in text
        assert "rm -rf" not in text
    assert ": remove" not in output
    assert ["systemctl", "--user", "stop", templates.MCP_UNIT] not in runner.calls
    unit = (host.unit_dir / templates.MCP_UNIT).read_text()
    assert f"ExecStart={host.new}/bin/proton-mail-mcp-service " in unit
    # A rerun only repeats the reminder.
    output, _runner = apply(host, venv=None)
    assert snapshot(host.old) == before
    assert installer_log(host).count(f"The previous virtual environment {host.old} (") == 2


def bridge_unit(*lines: str) -> str:
    return "\n".join(["[Unit]", "Description=Bridge, as the user left it", "[Service]", *lines, ""])


@pytest.mark.parametrize(
    "case",
    ["xdg-specifier", "relative-argument", "relative-variable", "escape"],
)
def test_r7_unit_values_never_cost_the_old_venv_on_disk(tmp_path, case):
    # On disk: a Bridge unit left in place runs a script from the old
    # venv through %C resolved from the shell's XDG_CACHE_HOME, a relative argument or
    # variable, or a \uNNNN escape. Nothing parses it now, and nothing is deleted.
    home = tmp_path / "home"
    old = {
        "xdg-specifier": home / ".cache" / "old-venv",
        "relative-argument": home / "venv",
        "relative-variable": home / "venv",
        "escape": tmp_path / "old-venv",
    }[case]
    host = installed(tmp_path, old=old, data_in_venv=False)
    lines = {
        "xdg-specifier": ["ExecStart=/bin/sh %C/old-venv/start.sh"],
        "relative-argument": ["ExecStart=/bin/sh venv/start-bridge.sh"],
        "relative-variable": ["Environment=X=venv", "ExecStart=/bin/sh $X/start.sh"],
        "escape": [f"ExecStart={tmp_path}/\\u006fld-venv/start.sh"],
    }[case]
    if case == "xdg-specifier":
        # The installing shell's cache directory differs; the user manager's does not.
        host.probe.env["XDG_CACHE_HOME"] = str(tmp_path / "install-cache")
        host.paths = paths_for(simulated("debian"), host.probe.env, host.probe.home)
    write(old / "start.sh", "#!/bin/sh\nexec bridge\n")
    write(old / "start-bridge.sh", "#!/bin/sh\nexec bridge\n")
    unit = write(host.unit_dir / templates.BRIDGE_UNIT, bridge_unit(*lines))
    before, definition = snapshot(old), unit.read_bytes()
    output, _runner = apply(host, skip_bridge_service=True)
    assert snapshot(old) == before and unit.read_bytes() == definition
    assert host.settings()[PREVIOUS_VENVS_KEY] == f'["{old}"]'
    for text in (output, installer_log(host)):
        assert f"The previous virtual environment {old} (" in text
        assert "has not checked whether anything else of yours runs from it" in text
        assert f"remove it with: `rm -rf -- {old}`" in text


@pytest.mark.parametrize("case", ["disable-fails-unit-active", "unit-still-activating"])
def test_r7_uninstall_removes_nothing_until_every_stop_is_proven(tmp_path, case):
    # `disable --now` failed (RefuseManualStop=yes), uninstall went on, and
    # the units, settings and venv were removed under a running service.
    host = installed(tmp_path)
    downloads(host)
    if case == "disable-fails-unit-active":
        results = {
            ("systemctl", "--user", "disable"): (1, ""),
            (*ACTIVE_STATE, templates.MCP_UNIT): (0, "active\n"),
        }
    else:
        results = {(*ACTIVE_STATE, templates.BRIDGE_UNIT): (0, "activating\n")}
    before = snapshot(host.root)
    output, runner, error = execute(plan(host, uninstall_plan), Runner(results))
    assert error is not None and STOP_ADVICE in str(error)
    assert str(error).endswith("then rerun uninstall.")
    # Units, settings file, venv, downloads and data: every byte stays.
    assert snapshot(host.root) == before
    assert ": remove" not in output
    assert not any("daemon-reload" in argv for argv in runner.calls)
    # Once the units stop, a rerun goes through.
    output, _runner = apply(host, action=uninstall_plan)
    assert not (host.unit_dir / templates.MCP_UNIT).exists()
    assert not Path(host.paths.env_file).exists()
    assert snapshot(host.old) == before_old(before, host)


def before_old(before: dict[str, object], host: Host) -> dict[str, object]:
    """The part of a snapshot of host.root that lies in the old venv."""
    prefix = host.old.relative_to(host.root).as_posix()
    return {
        "." if name == prefix else name.removeprefix(prefix + "/"): value
        for name, value in before.items()
        if name == prefix or name.startswith(prefix + "/")
    }


@pytest.mark.parametrize("answer", [(0, "state = running\n"), (1, ""), (-9, "")])
def test_r7_a_macos_agent_still_loaded_after_bootout_keeps_everything(tmp_path, answer):
    # On macOS: a failed bootout was ignored. `launchctl print` must report
    # the agent not found before any plist or the settings file goes; a query that
    # fails otherwise, or dies of a signal, proves nothing.
    home = tmp_path / "home"
    home.mkdir()
    probe = DiskProbe(home)
    platform = simulated("macos")
    paths = paths_for(platform, probe.env, probe.home)
    for label, plist in (
        (templates.MCP_LABEL, templates.launchd_mcp_plist("/x/svc", paths.env_file, "/x", "/x")),
        (templates.BRIDGE_LABEL, templates.launchd_bridge_plist("/x/bridge", "/x")),
    ):
        path = write(Path(paths.agents_dir) / f"{label}.plist", plist)
        probe.launchd[label] = str(path)
    write(Path(paths.venv) / DIR_MARKER, MARKER)
    write(Path(paths.env_file), f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\n")
    steps = uninstall_plan(platform, Options(bridge_user=USER), probe, paths)
    still = {("launchctl", "print", f"gui/1000/{templates.MCP_LABEL}"): answer}
    before = snapshot(tmp_path)
    output, runner, error = execute(steps, Runner(still))
    assert error is not None
    assert f"Stop it yourself with `launchctl bootout gui/1000/{templates.MCP_LABEL}`" in str(error)
    assert snapshot(tmp_path) == before and ": remove" not in output
    assert ["launchctl", "bootout", f"gui/1000/{templates.MCP_LABEL}"] in runner.calls
    # Unloaded for real, a rerun removes the plists and the settings, nothing else.
    probe.launchd.clear()
    output, _runner, error = execute(uninstall_plan(platform, Options(), probe, paths))
    assert error is None, output
    assert not list(Path(paths.agents_dir).iterdir()) and not Path(paths.env_file).exists()
    assert (Path(paths.venv) / DIR_MARKER).is_file()


@pytest.mark.parametrize("fragment", [f"/etc/systemd/user/{templates.MCP_UNIT}", None])
def test_r7_a_foreign_mcp_unit_stops_uninstall_before_anything_changes(tmp_path, fragment):
    # /etc/systemd/user/proton-mail-mcp.service, which uninstall leaves
    # running, runs the installer's venv; uninstall removed the venv under it.
    # Neither that nor a unit systemd does not name is stopped, and nothing goes.
    host = installed(tmp_path)
    downloads(host)
    if fragment:
        (host.unit_dir / templates.MCP_UNIT).unlink()
    host.probe.fragments[templates.MCP_UNIT] = fragment
    before = snapshot(host.root)
    with pytest.raises(PlanError) as refused:
        apply(host, action=uninstall_plan)
    assert "changes nothing" in str(refused.value)
    assert f"systemctl --user status {templates.MCP_UNIT}" in str(refused.value)
    assert snapshot(host.root) == before


def test_r7_a_replaced_gnupg_home_keeps_its_linked_keys(tmp_path):
    # The saved GnuPG home's private-keys-v1.d links into the old venv;
    # exporting another GNUPGHOME dropped its protection and the keys went with the
    # venv. Nothing is deleted now; the change is reported as before.
    host = installed(tmp_path, data_in_venv=False)
    keys = write(host.old / "keys" / "ABCDEF.key", os.urandom(300)).parent
    gnupg = tmp_path / "ext" / "gnupg"
    write(gnupg / "pubring.kbx", os.urandom(100))
    (gnupg / "private-keys-v1.d").symlink_to(keys, target_is_directory=True)
    lines = Path(host.paths.env_file).read_text().splitlines()
    lines = [line for line in lines if not line.startswith("PROTON_MCP_GNUPGHOME=")]
    Path(host.paths.env_file).write_text("\n".join([*lines, f"PROTON_MCP_GNUPGHOME={gnupg}", ""]))
    other = tmp_path / "other-gnupg"
    host.probe.env["GNUPGHOME"] = str(other)
    before = snapshot(keys), snapshot(gnupg)
    output, _runner = apply(host)
    assert (snapshot(keys), snapshot(gnupg)) == before
    assert f"PROTON_MCP_GNUPGHOME changes from {gnupg} to {other}" in output
    assert host.settings()["PROTON_MCP_GNUPGHOME"] == str(other)
    # The note never calls the old venv unused, links included.
    assert "keeps data in it or links into it" in output


def test_r7_no_secret_from_a_definition_reaches_output_or_the_log(tmp_path):
    # An unresolvable command reached the report whole, password and all.
    # Seeded in a unit (a quoted absolute command through a symlink loop), a drop-in,
    # an EnvironmentFile and the manager's environment, the secret reaches nothing.
    host = installed(tmp_path)
    loop = tmp_path / "loop"
    loop.mkdir()
    (loop / "a").symlink_to(loop / "b")
    (loop / "b").symlink_to(loop / "a")
    tool_env = write(host.root / "home" / ".config" / "tool.env", f"TOOL_PASSWORD={SECRET}\n")
    write(
        host.unit_dir / "tool.service",
        f'[Service]\nExecStart="{loop}/a/tool --password {SECRET}"\nEnvironmentFile={tool_env}\n',
    )
    write(
        host.unit_dir / "tool.service.d" / "10-env.conf",
        f'[Service]\nEnvironment="TOOL_DB=postgres://tool:{SECRET}@localhost/db"\n',
    )
    host.probe.manager = f"HOME={host.probe.home}\nTOOL_TOKEN={SECRET}\n"
    moved, _runner = apply(host)
    removed, _runner = apply(host, action=uninstall_plan)
    log = installer_log(host)
    for text in (moved, removed, log):
        assert SECRET not in text and "--password" not in text and "postgres://" not in text
    assert f"The previous virtual environment {host.old} (" in log
    assert f"The virtual environment {host.new} (" in log


@pytest.mark.parametrize("unusable", ["no-systemctl-on-path", "no-user-manager"])
def test_uninstall_without_a_usable_systemctl_changes_nothing(tmp_path, unusable):
    # A restricted PATH was taken as proof the units were stopped; the units and
    # the venv were removed while they might still run.
    host = installed(tmp_path)
    if unusable == "no-systemctl-on-path":
        host.probe.missing.add("systemctl")
    else:
        host.probe.systemd = False
        host.probe.exists = lambda path: path == SYSTEMD_BOOTED or os.path.exists(path)
    before = snapshot(host.root)
    with pytest.raises(PlanError) as refused:
        apply(host, action=uninstall_plan)
    message = str(refused.value)
    assert "Nothing was changed" in message
    assert f"systemctl --user disable --now {templates.MCP_UNIT} {templates.BRIDGE_UNIT}" in message
    assert str(host.unit_dir / templates.MCP_UNIT) in message
    assert "prints how to remove the virtual environment" in message
    assert snapshot(host.root) == before


def test_r7_upgrade_from_the_readme_setup_loses_no_byte(tmp_path):
    """From 8855fbd's README setup: install, a --venv move, uninstall, uninstall again.

    The README left a venv inside the checkout (never the installer's), the server's
    state at its defaults, ~/.gnupg and ~/.password-store. After every step all of
    that is byte for byte as it was; after uninstall the installer's venvs and the
    downloads are too, and only the units and the settings file are gone.
    """
    home = tmp_path / "home"
    checkout = home / "src" / "proton-mail-mcp"
    write(checkout / "pyproject.toml", '[project]\nname = "proton-mail-mcp"\n')
    write(checkout / ".venv" / "pyvenv.cfg", "home = /usr/bin\n")
    write(checkout / ".venv" / "bin" / "proton-mail-mcp", "#!/bin/sh\n")
    state = home / ".local" / "state" / "proton-mail-mcp"
    for name, content in DATA.items():
        if name.startswith("audit/"):
            write(state / name.removeprefix("audit/"), content)
        elif name.startswith("identity-index/"):
            write(state / name, content)
    for name in ("pubring.kbx", "private-keys-v1.d/ABCDEF.key"):
        write(home / ".gnupg" / name, DATA[f"gnupg/{name}"])
    for name in [name for name in DATA if name.startswith("pass/")]:
        write(home / ".password-store" / name.removeprefix("pass/"), DATA[name])
    bridge = write(tmp_path / "bridge" / "protonmail-bridge", "#!/bin/sh\n")
    probe = DiskProbe(home)
    platform = simulated("debian")
    paths = paths_for(platform, probe.env, probe.home)
    # The state directory also receives installer.log; the server's own files count.
    items = ("actions.jsonl", "rules-state.json", "saved-rules.json", "search-index")
    user = {
        **{f"state {item}": state / item for item in (*items, "identity-index")},
        "gnupg": home / ".gnupg",
        "pass": home / ".password-store",
        "checkout venv": checkout / ".venv",
    }
    before = {name: snapshot(path) for name, path in user.items()}

    def run(action, **overrides):
        values = {"bridge_user": USER, "python": sys.executable}
        if action is install_plan:
            values.update(
                source=str(checkout),
                skip_bridge=True,
                skip_credentials=True,
                bridge_bin=str(bridge),
                credential_store="pass",
            )
        steps = action(platform, Options(**{**values, **overrides}), probe, paths)
        output, _runner, error = execute(steps)
        assert error is None, f"{error}\n{output}"
        assert {name: snapshot(path) for name, path in user.items()} == before
        return output

    run(install_plan)
    default = Path(paths.venv)
    assert (default / DIR_MARKER).is_file()
    settings = parse_env_file(Path(paths.env_file).read_text())
    assert settings["PROTON_MCP_ACTION_LOG"] == str(state / "actions.jsonl")
    assert settings["PROTON_MCP_PASSWORD_STORE_DIR"] == str(home / ".password-store")
    new = tmp_path / "venvs" / "new"
    moved = run(install_plan, venv=str(new))
    assert f"The previous virtual environment {default} (" in moved
    assert f"remove it with: `rm -rf -- {default}`" in moved
    cache = Path(paths.cache_dir)
    write(cache / DIR_MARKER, MARKER)
    write(cache / "bridge_pubkey.gpg", os.urandom(1800))
    kept = {path: snapshot(path) for path in (default, new, cache)}
    removed = run(uninstall_plan)
    assert {path: snapshot(path) for path in kept} == kept
    for unit in (templates.MCP_UNIT, templates.BRIDGE_UNIT):
        assert not (Path(paths.unit_dir) / unit).exists()
    assert not Path(paths.env_file).exists()
    for path, what in (
        (new, "The virtual environment"),
        (cache, "The download directory"),
        (default, "The previous virtual environment"),
    ):
        assert f"{what} {path} (" in removed
        assert f"remove it with: `rm -rf -- {path}`" in removed
    assert f"PROTON_MCP_ACTION_LOG={state / 'actions.jsonl'}" in removed
    # Uninstall again: nothing left to stop or remove, and still every byte stays.
    again = run(uninstall_plan)
    assert {path: snapshot(path) for path in kept} == kept
    assert ": remove" not in again
