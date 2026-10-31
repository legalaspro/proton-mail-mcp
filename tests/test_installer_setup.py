"""`setup`: the guided install, from an installed wheel, with its credential guarantees."""

import dataclasses
import email.message
import getpass
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from proton_mail_mcp import credential_store
from proton_mail_mcp.credential_store import BRIDGE_SERVICE, TOKEN_SERVICE
from proton_mail_mcp.installer import cli, execute, selfcheck, templates
from proton_mail_mcp.installer.detect import detect_host, simulated
from proton_mail_mcp.installer.execute import Executor, InstallError
from proton_mail_mcp.installer.plan import (
    BRIDGE_PROCESSES,
    MANAGER_ENVIRONMENT,
    SYSTEMD_BOOTED,
    Options,
    PlanError,
    install_plan,
    paths_for,
)
from proton_mail_mcp.installer.probe import FakeProbe, HostProbe, simulated_probe
from proton_mail_mcp.installer.steps import (
    MARKER,
    BridgeLogin,
    BridgeService,
    CheckSameCopy,
    CheckStopped,
    Command,
    EnsureDir,
    Note,
    SelfCheck,
    StoreSecret,
    WaitForPort,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run
USER = "you@example.com"
PASSWORD = "bridge-password-for-this-test"
TOKEN = "t" * 43
FAMILIES = ("debian", "fedora", "arch", "macos")
# Before the fixture below: a child process sees the real one.
REAL_EUID = os.geteuid() if hasattr(os, "geteuid") else None


def options(**overrides):
    values = {
        "bridge_user": USER,
        "source": "/src/checkout",
        "python": "/usr/bin/python3",
        "setup": True,
    }
    return Options(**{**values, **overrides})


def plan(family="debian", probe=None, **overrides):
    platform = simulated(family)
    probe = probe or simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    return install_plan(platform, options(**overrides), probe, paths)


def kinds(steps, kind):
    return [step for step in steps if isinstance(step, kind)]


def position(steps, kind):
    return next(index for index, step in enumerate(steps) if isinstance(step, kind))


@pytest.fixture(autouse=True)
def normal_user(monkeypatch):
    # Hosted CI containers run pytest as root, which the installer refuses.
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)


@pytest.fixture
def host(monkeypatch, tmp_path):
    """The real-host code path on a simulated Debian machine whose home is tmp_path."""
    if os.name != "posix":
        pytest.skip("the executor writes these POSIX paths for real")
    platform = simulated("debian")
    probe = simulated_probe(platform)
    probe.home = str(tmp_path / "home")
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", lambda _platform: probe)
    monkeypatch.setattr(cli, "terminal_available", lambda: True)
    monkeypatch.setattr(cli, "default_source", lambda: "/src/checkout")
    return SimpleNamespace(platform=platform, probe=probe, home=Path(probe.home))


def answers(monkeypatch, *replies, on_terminal=True):
    """Script input(); a question beyond the replies fails the test."""
    asked = []
    pending = list(replies)

    def fake(prompt=""):
        asked.append(prompt)
        assert pending, f"unexpected question: {prompt}"
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake)
    monkeypatch.setattr(cli, "on_terminal", lambda: on_terminal)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: on_terminal, raising=False)
    return asked


def no_real_run(monkeypatch):
    def factory(*, dry_run, **kwargs):
        assert dry_run, "this path must not apply anything"
        return Executor(dry_run=dry_run, **kwargs)

    monkeypatch.setattr(cli, "Executor", factory)


# The plan -------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_setup_dry_run_shows_every_stage_and_changes_nothing(family, capsys, monkeypatch):
    no_real_run(monkeypatch)
    code = cli.main(["setup", "--dry-run", "--simulate", family, "--bridge-user", USER])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("setup plan for ")
    for stage in (
        "Offer Bridge's CLI to log in",
        "Prompt for the Bridge password",
        "Generate the MCP bearer token",
        "Check Bridge listens on 127.0.0.1",
        "Check the MCP server answers and reads mail through Bridge",
    ):
        assert stage in out, stage
    assert out.rstrip().endswith("Dry run: nothing was changed.")


def test_setup_plan_order_login_then_password_then_services_then_self_check():
    steps = plan()
    assert position(steps, BridgeLogin) < position(steps, StoreSecret)
    assert steps[-1] == kinds(steps, SelfCheck)[0]
    assert isinstance(steps[-2], WaitForPort) and steps[-2].port == 1143
    started = [
        index
        for index, step in enumerate(steps)
        if isinstance(step, Command) and step.argv[2:3] == ("start",)
    ]
    assert position(steps, StoreSecret) < started[0]
    login = kinds(steps, BridgeLogin)[0]
    assert login.argv == ("/usr/bin/protonmail-bridge", "--cli")


def test_the_virtual_environment_is_checked_before_any_prompt():
    steps = plan()
    check = next(
        index
        for index, step in enumerate(steps)
        if isinstance(step, Command) and "import proton_mail_mcp.server" in step.argv
    )
    assert check < position(steps, BridgeLogin)
    # -I: this environment's copy, not a checkout on the installer's PYTHONPATH.
    assert steps[check].argv[1] == "-I"


def test_install_keeps_its_plan_without_setup_stages():
    steps = plan(setup=False)
    assert not kinds(steps, BridgeLogin) and not kinds(steps, SelfCheck)
    assert not any("import proton_mail_mcp.server" in getattr(s, "argv", ()) for s in steps)


@pytest.mark.parametrize("flag", ["skip_bridge", "skip_bridge_login"])
def test_no_login_offer_when_bridge_is_not_ours_to_open(flag):
    probe = simulated_probe(simulated("debian"))
    probe.files["/usr/bin/protonmail-bridge"] = ""
    assert not kinds(plan(probe=probe, **{flag: True}), BridgeLogin)


@pytest.mark.parametrize(
    ("flag", "text"), [("skip_services", "no service"), ("skip_credentials", "stored token")]
)
def test_the_self_check_is_skipped_with_a_reason_when_it_cannot_run(flag, text):
    steps = plan(**{flag: True})
    assert not kinds(steps, SelfCheck)
    assert any(text in step.text for step in kinds(steps, Note) if "Self-check" in step.text)


def test_a_host_without_systemd_gets_no_self_check_of_a_service():
    probe = simulated_probe(simulated("fedora"))
    del probe.files[SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    steps = plan("fedora", probe)
    assert not kinds(steps, SelfCheck)
    assert any("Self-check: no service" in step.text for step in kinds(steps, Note))


def test_self_check_points_at_each_platforms_logs():
    assert "journalctl --user -u proton-mail-mcp.service" == kinds(plan(), SelfCheck)[0].hint
    assert kinds(plan("macos"), SelfCheck)[0].hint.endswith("/Logs/proton-mail-mcp/stderr.log")


def test_self_check_reads_the_token_the_service_reads():
    probe = simulated_probe(simulated("debian"))
    probe.files["/home/you/.password-store/.gpg-id"] = "key\n"
    steps = plan(probe=probe, credential_store="pass")
    check = kinds(steps, SelfCheck)[0]
    assert (check.store, check.service, check.account) == ("pass", TOKEN_SERVICE, USER)
    assert dict(check.locations) == {"PASSWORD_STORE_DIR": "/home/you/.password-store"}


def test_the_plan_names_the_private_venv_for_show_token():
    # uvx leaves nothing on PATH, so the note gives a command that stays.
    notes = " ".join(step.text for step in kinds(plan(), Note))
    assert "`/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp show-token`" in notes


def test_a_python_without_ensurepip_stops_before_anything_changes():
    # venv would leave a Python without pip behind, which a rerun would reuse and fail on.
    probe = simulated_probe(simulated("debian"))
    probe.venv_module = False
    with pytest.raises(PlanError, match="sudo apt-get install python3-venv"):
        plan(probe=probe)
    probe = simulated_probe(simulated("fedora"))
    probe.venv_module = False
    with pytest.raises(PlanError, match="ensurepip"):
        plan("fedora", probe)


def test_an_existing_venv_needs_no_ensurepip():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.venv_module = False
    probe.files[paths.venv_python] = ""
    probe.files[f"{paths.venv}/.proton-mail-mcp-installer"] = ""
    steps = plan(probe=probe)
    assert not any(isinstance(step, Command) and step.argv[1:3] == ("-m", "venv") for step in steps)


def test_host_probe_asks_the_python_itself_about_ensurepip(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert HostProbe(simulated("debian")).can_make_venv("/opt/python") is False
    assert calls == [["/opt/python", "-c", "import ensurepip"]]


# Source: an installed wheel needs no checkout ---------------------------------------


class Installed:
    def __init__(self, version="1.2.3", origin=None):
        self.version, self.origin = version, origin

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.origin


def installed(monkeypatch, distribution):
    def find(name):
        assert name == "proton-mail-mcp"
        if distribution is None:
            raise cli.metadata.PackageNotFoundError
        return distribution

    monkeypatch.setattr(cli.metadata, "distribution", find)


def test_an_index_install_pins_its_own_version(monkeypatch):
    installed(monkeypatch, Installed())
    assert cli.installed_source() == "proton-mail-mcp==1.2.3"


def test_a_local_wheel_is_reinstalled_from_that_wheel_and_never_the_index(monkeypatch, tmp_path):
    # The index could hold something else under this name.
    wheel = tmp_path / "proton_mail_mcp-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"")
    origin = json.dumps({"url": wheel.as_uri(), "archive_info": {}})
    installed(monkeypatch, Installed(origin=origin))
    assert cli.installed_source() == str(wheel)


@pytest.mark.parametrize(
    "origin",
    [
        # The file is gone: refuse rather than fall back to the index.
        json.dumps({"url": "file:///gone/proton_mail_mcp-1.2.3-py3-none-any.whl"}),
        # Another origin, whose URL may hold credentials pip would print.
        json.dumps({"url": "https://user:secret@example.com/x.whl", "archive_info": {}}),
        json.dumps({"url": "ssh://git@example.com/x", "vcs_info": {"vcs": "git"}}),
        json.dumps({}),
        json.dumps(["not a mapping"]),
        "not json",
    ],
)
def test_an_origin_it_cannot_name_is_unknown(monkeypatch, origin):
    installed(monkeypatch, Installed(origin=origin))
    assert cli.installed_source() == ""


def test_no_distribution_at_all_is_unknown(monkeypatch):
    installed(monkeypatch, None)
    assert cli.installed_source() == ""


def test_a_wheel_install_finds_no_checkout_beside_it(monkeypatch, tmp_path):
    module = tmp_path / "lib" / "python3.12" / "site-packages" / "proton_mail_mcp"
    monkeypatch.setattr(cli, "__file__", str(module / "installer" / "cli.py"))
    installed(monkeypatch, Installed())
    assert cli.default_source() == "proton-mail-mcp==1.2.3"


def test_an_unknown_source_says_how_to_name_one(monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_source", lambda: "")
    argv = ["setup", "--dry-run", "--simulate", "arch", "--bridge-user", USER]
    assert cli.main(argv) == 2
    assert "--source with a checkout or a wheel file" in capsys.readouterr().err


def test_source_accepts_a_checkout_or_this_packages_wheel_and_nothing_else(tmp_path, capsys):
    wheel = tmp_path / "proton_mail_mcp-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    base = ["setup", "--dry-run", "--simulate", "arch", "--bridge-user", USER]
    assert cli.main([*base, "--source", str(wheel)]) == 0
    assert f"Install proton-mail-mcp from {wheel}" in capsys.readouterr().out
    assert cli.main([*base, "--source", str(ROOT)]) == 0
    capsys.readouterr()
    other = tmp_path / "other-1.0.0-py3-none-any.whl"
    other.write_bytes(b"")
    for source in (str(other), str(tmp_path), str(tmp_path / "missing.whl")):
        with pytest.raises(SystemExit) as caught:
            cli.main([*base, "--source", source])
        assert caught.value.code == 2
        assert "checkout or wheel file" in capsys.readouterr().err


def test_a_wheel_source_is_always_reinstalled_so_a_rebuild_takes_effect():
    pip = lambda steps: next(s.argv for s in kinds(steps, Command) if "pip" in s.argv)  # noqa: E731
    assert "--force-reinstall" in pip(plan(source="/w/proton_mail_mcp-1.0.0-py3-none-any.whl"))
    assert "--force-reinstall" not in pip(plan(source="/src/checkout"))
    assert "--force-reinstall" not in pip(plan(source="proton-mail-mcp==1.0.0"))


def test_a_relative_source_reaches_pip_as_a_path(tmp_path, monkeypatch, capsys):
    # A bare name would otherwise be a package name to look up on the index.
    monkeypatch.chdir(tmp_path)
    wheel = tmp_path / "proton_mail_mcp-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    argv = ["setup", "--dry-run", "--simulate", "arch", "--bridge-user", USER]
    assert cli.main([*argv, "--source", wheel.name]) == 0
    assert f"Install proton-mail-mcp from {os.path.abspath(wheel.name)}" in capsys.readouterr().out


# Source: what pipx and uv recorded about the index ---------------------------------


PIN = "proton-mail-mcp==1.2.3"
INDEX = "https://mirror.example/simple/"
OTHER = "https://other.example/simple"
LINKS = "/srv/wheels"
SECRET_WORD = "s3cr3t-index-password"


def pipx_record(*pip_args):
    return json.dumps(
        {
            "main_package": {"package": "proton-mail-mcp", "pip_args": list(pip_args)},
            "pipx_metadata_version": "0.12",
        }
    )


def uv_record(options=""):
    return (
        '[tool]\nrequirements = [{ name = "proton-mail-mcp" }]\n'
        'entrypoints = [{ name = "proton-mail-mcp", install-path = "/b/proton-mail-mcp" }]\n'
        + (f"\n[tool.options]\n{options}\n" if options else "")
    )


@pytest.fixture
def environment(monkeypatch, tmp_path):
    """The environment this copy runs from, which pipx or uv may have left a record in."""
    root = tmp_path / "tool-environment"
    root.mkdir()
    monkeypatch.setattr(cli.sys, "prefix", str(root))
    return root


def record_pipx(environment, *pip_args):
    (environment / "pipx_metadata.json").write_text(pipx_record(*pip_args))


def record_uv(environment, options=""):
    (environment / "uv-receipt.toml").write_text(uv_record(options))


def test_plain_pip_and_uvx_keep_no_record_of_an_index(environment):
    assert cli.recorded_index_args(PIN) == ()


def test_pipx_with_no_index_options_adds_none(environment):
    # pip's own pip.conf and PIP_* variables reach the new pip by themselves.
    record_pipx(environment)
    assert cli.recorded_index_args(PIN) == ()


@pytest.mark.parametrize(
    ("recorded", "carried"),
    [
        # What real pipx 1.18 wrote for --index-url and --pip-args.
        (
            ["--index-url", INDEX, "--extra-index-url", OTHER, "--trusted-host", "mirror.example"],
            ["--index-url", INDEX, "--extra-index-url", OTHER, "--trusted-host", "mirror.example"],
        ),
        (["--find-links", LINKS], ["--find-links", LINKS]),
        # The spellings pip accepts, reduced to one.
        (["-i", INDEX], ["--index-url", INDEX]),
        ([f"-i{INDEX}"], ["--index-url", INDEX]),
        ([f"--index-url={INDEX}"], ["--index-url", INDEX]),
        (["--index", INDEX], ["--index-url", INDEX]),
        (
            ["-f", LINKS, f"--find-links=file://{LINKS}"],
            ["--find-links", LINKS, "--find-links", f"file://{LINKS}"],
        ),
        (["--find-links", f"{OTHER}/links"], ["--find-links", f"{OTHER}/links"]),
        (["--trusted-host", "[::1]:8080"], ["--trusted-host", "[::1]:8080"]),
        (["--no-index", "--find-links", LINKS], ["--find-links", LINKS, "--no-index"]),
        # pip's own rule: the last --index-url wins.
        (["-i", OTHER, "-i", INDEX], ["--index-url", INDEX]),
        # Options that do not say where a package comes from stay behind.
        (["--no-cache-dir", "--timeout", "30", "-v", "--pre"], []),
    ],
)
def test_pipx_index_options_are_carried(environment, recorded, carried):
    record_pipx(environment, *recorded)
    assert cli.recorded_index_args(PIN) == tuple(carried)


@pytest.mark.parametrize(
    ("options", "carried"),
    [
        ("", []),
        # What real uv 0.11 wrote, from flags, UV_* variables and uv.toml alike.
        (
            f'index-url = "{INDEX}"\nextra-index-url = ["{OTHER}"]\n'
            'index-strategy = "unsafe-best-match"',
            ["--index-url", INDEX, "--extra-index-url", OTHER],
        ),
        (f'find-links = ["file://{LINKS}"]', ["--find-links", f"file://{LINKS}"]),
        (
            f'index = [{{ url = "{INDEX}", explicit = false, default = true, format = "simple", '
            f'authenticate = "auto" }}, {{ url = "{OTHER}", explicit = false, default = false, '
            'format = "simple", authenticate = "auto" }]',
            ["--index-url", INDEX, "--extra-index-url", OTHER],
        ),
        (f'index = [{{ url = "{OTHER}", format = "flat" }}]', ["--find-links", OTHER]),
        # uv itself uses an explicit index only for a package pinned to it.
        (f'index = [{{ url = "{OTHER}", explicit = true }}]', []),
        (f'no-index = true\nfind-links = ["{LINKS}"]', ["--find-links", LINKS, "--no-index"]),
    ],
)
def test_uv_tool_index_options_are_carried(environment, options, carried):
    record_uv(environment, options)
    assert cli.recorded_index_args(PIN) == tuple(carried)


@pytest.mark.parametrize(
    "recorded",
    [
        # What real pipx 1.18 keeps for --index-url with credentials: they stay in the file.
        ["--index-url", f"https://user:{SECRET_WORD}@mirror.example/simple/"],
        ["--extra-index-url", f"https://{SECRET_WORD}@mirror.example/simple/"],
        ["--find-links", f"https://user:{SECRET_WORD}@mirror.example/links/"],
        ["--index-url", f"https://mirror.example/simple/?token={SECRET_WORD}"],
        ["--index-url", f"https://mirror.example/simple/#{SECRET_WORD}"],
        ["--trusted-host", f"user:{SECRET_WORD}@mirror.example"],
        # Relative to a directory nobody recorded.
        ["--find-links", f"./{SECRET_WORD}"],
        ["--find-links", SECRET_WORD],
        ["-f", f"../{SECRET_WORD}"],
        ["--index-url", f"ftp://mirror.example/{SECRET_WORD}"],
        ["--index-url", f"/srv/{SECRET_WORD}"],
        ["--index-url", f"https://[{SECRET_WORD}/simple/"],
        ["--index-url", f"https://mirror.example/{SECRET_WORD}\n/simple/"],
    ],
)
def test_a_record_that_cannot_be_carried_stops_the_plan_and_is_not_shown(environment, recorded):
    record_pipx(environment, *recorded)
    with pytest.raises(PlanError) as caught:
        cli.recorded_index_args(PIN)
    assert "--source with a checkout or a wheel file" in str(caught.value)
    assert SECRET_WORD not in str(caught.value)


@pytest.mark.parametrize(
    "options",
    [
        f'index-url = "https://user:{SECRET_WORD}@mirror.example/simple/"',
        f'find-links = ["./{SECRET_WORD}"]',
        f'index = [{{ url = "https://mirror.example/?{SECRET_WORD}", default = true }}]',
    ],
)
def test_a_uv_record_that_cannot_be_carried_stops_the_plan(environment, options):
    record_uv(environment, options)
    with pytest.raises(PlanError) as caught:
        cli.recorded_index_args(PIN)
    assert "--source with a checkout or a wheel file" in str(caught.value)
    assert SECRET_WORD not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pipx_metadata.json", "not json"),
        ("pipx_metadata.json", "[]"),
        ("pipx_metadata.json", "{}"),
        ("pipx_metadata.json", '{"main_package": {}}'),
        ("pipx_metadata.json", '{"main_package": {"pip_args": "--index-url x"}}'),
        ("pipx_metadata.json", '{"main_package": {"pip_args": [1]}}'),
        # An option with its value missing.
        ("pipx_metadata.json", '{"main_package": {"pip_args": ["--index-url"]}}'),
        ("uv-receipt.toml", "not = [toml"),
        ("uv-receipt.toml", "other = 1"),
        ("uv-receipt.toml", "tool = 1"),
        ("uv-receipt.toml", '[tool]\noptions = "x"'),
        ("uv-receipt.toml", "[tool.options]\nindex-url = 1"),
        ("uv-receipt.toml", '[tool.options]\nextra-index-url = "https://x.example/"'),
        ("uv-receipt.toml", "[tool.options]\nindex = [1]"),
        ("uv-receipt.toml", "[tool.options]\nindex = [{ default = true }]"),
    ],
)
def test_a_record_it_cannot_read_is_never_taken_for_no_record(environment, name, text):
    (environment / name).write_text(text)
    with pytest.raises(PlanError, match="--source with a checkout or a wheel file"):
        cli.recorded_index_args(PIN)


def test_a_record_that_is_not_text_stops_the_plan(environment):
    (environment / "pipx_metadata.json").write_bytes(b"\xff\xfe")
    with pytest.raises(PlanError, match="--source with a checkout or a wheel file"):
        cli.recorded_index_args(PIN)


@pytest.fixture
def pipx_installed(monkeypatch, tmp_path, environment):
    """Running from a pipx-made environment, installed by name; no checkout beside it."""
    module = tmp_path / "lib" / "python3.12" / "site-packages" / "proton_mail_mcp"
    monkeypatch.setattr(cli, "__file__", str(module / "installer" / "cli.py"))
    installed(monkeypatch, Installed())
    return environment


DRY = ["setup", "--dry-run", "--simulate", "arch", "--bridge-user", USER]


def test_the_recorded_index_reaches_pip_in_the_plan(pipx_installed, capsys):
    record_pipx(pipx_installed, "--index-url", INDEX, "--find-links", LINKS)
    assert cli.main(DRY) == 0
    out = capsys.readouterr().out
    assert f"--only-binary=proton-mail-mcp --index-url {INDEX} --find-links {LINKS} {PIN}" in out


def test_a_record_that_cannot_be_carried_changes_nothing_and_says_how(pipx_installed, capsys):
    record_pipx(pipx_installed, "--index-url", f"https://u:{SECRET_WORD}@mirror.example/simple/")
    assert cli.main(DRY) == 2
    captured = capsys.readouterr()
    assert "--source with a checkout or a wheel file" in captured.err
    assert f"pip download --no-deps {PIN}" in captured.err
    assert SECRET_WORD not in captured.out + captured.err
    assert "plan for" not in captured.out


def test_source_replaces_whatever_was_recorded(pipx_installed, tmp_path, capsys):
    # The way out of a refused record: no record is even read.
    (pipx_installed / "pipx_metadata.json").write_text("not json")
    wheel = tmp_path / "proton_mail_mcp-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"")
    assert cli.main([*DRY, "--source", str(wheel)]) == 0
    out = capsys.readouterr().out
    assert f"--force-reinstall {wheel}" in out
    assert "--only-binary" not in out and "holds the copy running" not in out


def test_a_recorded_index_is_not_used_for_a_checkout(environment, capsys):
    # This test runs from a checkout, which is the source whatever the environment says.
    record_pipx(environment, "--index-url", INDEX)
    assert cli.main(DRY) == 0
    out = capsys.readouterr().out
    assert INDEX not in out and "holds the copy running" not in out


def test_a_pinned_source_is_checked_before_anything_from_it_runs():
    steps = plan(source=PIN, source_args=("--index-url", INDEX))
    install = next(
        index
        for index, step in enumerate(steps)
        if isinstance(step, Command) and "pip" in step.argv
    )
    assert steps[install].argv[-5:] == (
        "--disable-pip-version-check",
        "--only-binary=proton-mail-mcp",
        "--index-url",
        INDEX,
        PIN,
    )
    # The next step that can run the venv's code is the import check, after this one.
    check = steps[install + 1]
    assert isinstance(check, CheckSameCopy)
    assert (check.python, check.venv) == (
        "/home/you/.local/share/proton-mail-mcp/venv/bin/python",
        "/home/you/.local/share/proton-mail-mcp/venv",
    )
    assert "--source" in check.hint and "--source" in steps[install].hint
    assert "import proton_mail_mcp.server" in steps[install + 2].argv


@pytest.mark.parametrize("source", ["/w/proton_mail_mcp-1.2.3-py3-none-any.whl", "/src/checkout"])
def test_a_path_source_needs_no_index_options_and_no_check(source):
    steps = plan(source=source)
    assert not kinds(steps, CheckSameCopy)
    install = next(step for step in kinds(steps, Command) if "pip" in step.argv)
    assert not any(word.startswith("--only-binary") for word in install.argv)
    assert install.hint == ""


def test_pip_finding_nothing_says_how_to_pass_the_source():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1)

    step = next(s for s in kinds(plan(source=PIN), Command) if "pip" in s.argv)
    executor = Executor(dry_run=False, out=io.StringIO(), runner=runner)
    with pytest.raises(InstallError) as caught:
        executor.run([step])
    message = str(caught.value)
    assert "exit status 1. If this copy came from another index" in message
    assert "--source with a checkout or a wheel file" in message
    assert f"pip download --no-deps {PIN}" in message


# The same copy, proven by its files --------------------------------------------------


def wheel_install(site, files, installer="pip", extra_rows=()):
    """A package as an installer leaves it in site-packages. Each row's hash names the
    installer, so only the files themselves can show two copies equal."""
    info = site / "proton_mail_mcp-1.2.3.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: proton-mail-mcp\nVersion: 1.2.3\n")
    (info / "entry_points.txt").write_text("[console_scripts]\nproton-mail-mcp = x:main\n")
    (info / "INSTALLER").write_text(installer)
    for name, content in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    own = [f"{info.name}/{name}" for name in ("METADATA", "entry_points.txt", "INSTALLER")]
    rows = [f"{name},sha256={installer},1" for name in (*files, *own)]
    # Every installer's own: a script with this environment's path, and bytecode.
    rows.append(f"../../../bin/proton-mail-mcp,sha256={installer},1")
    rows.append("proton_mail_mcp/__pycache__/server.cpython-312.pyc,,")
    rows.append(f"{info.name}/RECORD,,")
    rows.extend(extra_rows)
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return info


def distribution_at(site):
    return next(iter(execute.metadata.distributions(name="proton-mail-mcp", path=[str(site)])))


FILES = {"proton_mail_mcp/__init__.py": "x = 1\n", "proton_mail_mcp/ui/base.css": "a {}\n"}
SAME_COPY = CheckSameCopy("/venv/bin/python", "/venv", "Check it", hint="pass --source")


def copy_executor(tmp_path, target, *, returncode=0, stdout=None, dry_run=False):
    """The running copy at tmp_path/running; the venv's python only names `target`."""
    running = tmp_path / "running"
    if not running.exists():
        wheel_install(running, FILES, installer="uv")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, returncode, f"{target}\n" if stdout is None else stdout, ""
        )

    executor = Executor(
        dry_run=dry_run,
        out=io.StringIO(),
        runner=runner,
        running_copy=lambda: distribution_at(running),
    )
    return executor, calls


def test_the_same_files_from_another_installer_pass(tmp_path):
    # pip against a copy uv installed: their own files and scripts differ, the wheel's do not.
    target = tmp_path / "venv-site"
    wheel_install(
        target,
        FILES,
        installer="pip",
        extra_rows=["proton_mail_mcp-1.2.3.dist-info/direct_url.json,sha256=x,1"],
    )
    executor, calls = copy_executor(tmp_path, target)
    executor.run([SAME_COPY])
    assert "ok: the same files" in executor.out.getvalue()
    # -S: a .pth file of the copy not yet proven cannot run; nothing else ran at all.
    assert len(calls) == 1
    assert calls[0][0][:5] == ["/venv/bin/python", "-I", "-S", "-c", execute._SITE_PACKAGES]
    assert calls[0][0][5:] == ["/venv"]


def changed_file(site):
    (site / "proton_mail_mcp" / "__init__.py").write_text("x = 2\n")


def changed_metadata(site):
    (site / "proton_mail_mcp-1.2.3.dist-info" / "METADATA").write_text("Name: other\n")


def changed_entry_points(site):
    (site / "proton_mail_mcp-1.2.3.dist-info" / "entry_points.txt").write_text("[x]\n")


def missing_file(site):
    (site / "proton_mail_mcp" / "ui" / "base.css").unlink()


def unlisted_file(site):
    # Listed by pip even when the wheel's RECORD left it out; RECORD's hashes are not trusted.
    (site / "evil.pth").write_text("import os\n")
    record = site / "proton_mail_mcp-1.2.3.dist-info" / "RECORD"
    record.write_text(record.read_text() + "evil.pth,sha256=pip,1\n")


def no_record(site):
    (site / "proton_mail_mcp-1.2.3.dist-info" / "RECORD").unlink()


def malformed_record(site):
    (site / "proton_mail_mcp-1.2.3.dist-info" / "RECORD").write_text("just,two\n")


def second_version(site):
    wheel_install(site / "x", {})
    (site / "x" / "proton_mail_mcp-1.2.3.dist-info").rename(site / "proton_mail_mcp-9.dist-info")


def no_distribution(site):
    (site / "proton_mail_mcp-1.2.3.dist-info").rename(site / "other.txt")


@pytest.mark.parametrize(
    "change",
    [
        changed_file,
        changed_metadata,
        changed_entry_points,
        missing_file,
        unlisted_file,
        no_record,
        malformed_record,
        second_version,
        no_distribution,
    ],
)
def test_any_difference_stops_the_run_and_says_how_to_pass_the_source(tmp_path, change):
    target = tmp_path / "venv-site"
    wheel_install(target, FILES)
    change(target)
    executor, _calls = copy_executor(tmp_path, target)
    with pytest.raises(InstallError) as caught:
        executor.run([SAME_COPY])
    message = str(caught.value)
    assert "something other than the copy running this installer" in message
    assert "before any service is started" in message and "/venv holds" in message
    assert message.endswith("pass --source")


def test_a_running_copy_with_nothing_to_compare_proves_nothing(tmp_path):
    target = tmp_path / "venv-site"
    wheel_install(target, {})
    (target / "proton_mail_mcp-1.2.3.dist-info" / "RECORD").unlink()
    running = tmp_path / "running"
    wheel_install(running, {})
    (running / "proton_mail_mcp-1.2.3.dist-info" / "RECORD").unlink()
    executor, _calls = copy_executor(tmp_path, target)
    with pytest.raises(InstallError, match="something other than"):
        executor.run([SAME_COPY])


def test_a_copy_that_is_not_installed_cannot_be_compared(tmp_path):
    target = tmp_path / "venv-site"
    wheel_install(target, FILES)
    executor, _calls = copy_executor(tmp_path, target)

    def absent():
        raise execute.metadata.PackageNotFoundError

    executor.running_copy = absent
    with pytest.raises(InstallError, match="something other than"):
        executor.run([SAME_COPY])


@pytest.mark.parametrize(
    "settings",
    [{"returncode": 1}, {"stdout": ""}, {"stdout": "\n"}, {"returncode": 3, "stdout": ""}],
)
def test_a_python_that_does_not_name_its_packages_proves_nothing(tmp_path, monkeypatch, settings):
    # An empty name would mean the current directory; a copy planted there must not pass.
    target = tmp_path / "venv-site"
    wheel_install(target, FILES)
    wheel_install(tmp_path / "cwd", FILES)
    monkeypatch.chdir(tmp_path / "cwd")
    executor, _calls = copy_executor(tmp_path, target, **settings)
    with pytest.raises(InstallError, match="something other than"):
        executor.run([SAME_COPY])


@pytest.mark.parametrize("failure", [OSError("no python"), subprocess.TimeoutExpired("python", 60)])
def test_a_python_that_cannot_run_proves_nothing(tmp_path, failure):
    target = tmp_path / "venv-site"
    wheel_install(target, FILES)
    executor, _calls = copy_executor(tmp_path, target)

    def runner(*_args, **_kwargs):
        raise failure

    executor.runner = runner
    with pytest.raises(InstallError, match="something other than"):
        executor.run([SAME_COPY])


def test_a_dry_run_only_says_what_it_would_check(tmp_path):
    out = io.StringIO()
    executor = Executor(
        dry_run=True,
        out=out,
        runner=lambda *a, **k: pytest.fail("ran"),
        running_copy=lambda: pytest.fail("read the running copy"),
    )
    executor.run([SAME_COPY])
    # The plan shows this before anyone agrees to it.
    assert "checked when applied" in out.getvalue() and "pass --source" in out.getvalue()


@pytest.mark.skipif(os.name != "posix", reason="POSIX layout")
def test_the_site_packages_query_finds_a_real_venv_without_site(tmp_path):
    # Without site a venv's python thinks it is the base one, so the venv is named.
    venv = tmp_path / "venv"
    REAL_RUN([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
    python = venv / "bin" / "python"
    site = next((venv / "lib").glob("python*/site-packages"))
    # A .pth file that would announce itself if site ran.
    (site / "announce.pth").write_text("import sys; print('site ran', file=sys.stderr)\n")
    result = REAL_RUN(
        [str(python), "-I", "-S", "-c", execute._SITE_PACKAGES, str(venv)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.strip() == str(site)
    assert "site ran" not in result.stderr
    with_site = REAL_RUN(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert with_site.stdout.strip() == str(site) and "site ran" in with_site.stderr


# Asking a person -------------------------------------------------------------------


def test_a_dry_run_without_a_username_shows_a_placeholder_and_asks_no_store(host, capsys):
    assert cli.main(["setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"this plan shows {cli.PLACEHOLDER_ACCOUNT}" in out
    assert "setup asks for the username when applied" in out
    assert host.probe.secret_locations == []


def test_a_saved_username_is_used_without_asking(host, monkeypatch, capsys):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    host.probe.files[paths.env_file] = f"# {MARKER}\nPROTON_BRIDGE_USER=saved@example.com\n"
    answers(monkeypatch, on_terminal=True)
    assert cli.main(["setup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "saved@example.com" in out and cli.PLACEHOLDER_ACCOUNT not in out


def test_a_foreign_settings_file_stops_setup_like_install(host, capsys):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    host.probe.files[paths.env_file] = "PROTON_BRIDGE_USER=x@example.com\n"
    assert cli.main(["setup", "--dry-run"]) == 2
    assert "was not written by the installer" in capsys.readouterr().err


def test_applying_on_a_terminal_asks_for_the_username_then_for_consent(host, monkeypatch, capsys):
    asked = answers(monkeypatch, USER, "", "n")
    assert cli.main(["setup"]) == 1
    assert asked[0].startswith("Bridge IMAP username") and asked[2].startswith("Apply these steps")
    captured = capsys.readouterr()
    assert USER in captured.out and "Nothing was changed." in captured.out
    assert not host.home.exists()


def test_a_password_pasted_as_the_username_is_refused_and_never_repeated(host, monkeypatch, capsys):
    pasted = "bridgepasswordwithnoatsign"
    answers(monkeypatch, pasted, "pass@word", "two@at@signs.example")
    assert cli.main(["setup"]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert pasted not in output and "pass@word" not in output and "two@at" not in output
    assert "pass --bridge-user" in captured.err
    assert "not an address" in captured.err


@pytest.mark.parametrize("name", [USER, "first.last+tag@sub.proton.me", "a@b.co"])
def test_a_bridge_address_is_accepted(host, monkeypatch, capsys, name):
    answers(monkeypatch, name, "", "n")
    assert cli.main(["setup"]) == 1
    assert name in capsys.readouterr().out


def test_three_wrong_usernames_stop_before_any_plan(host, monkeypatch, capsys):
    answers(monkeypatch, "a", "b", "c")
    assert cli.main(["setup"]) == 2
    captured = capsys.readouterr()
    assert captured.err.count("not an address") == 3
    assert "plan for" not in captured.out


@pytest.mark.parametrize("mistake", [EOFError, KeyboardInterrupt])
def test_ending_the_username_question_changes_nothing(host, monkeypatch, capsys, mistake):
    monkeypatch.setattr(cli, "on_terminal", lambda: True)

    def stop(_prompt=""):
        raise mistake

    monkeypatch.setattr("builtins.input", stop)
    assert cli.main(["setup"]) == 130
    assert "nothing was changed" in capsys.readouterr().err
    assert not host.home.exists()


@pytest.mark.parametrize("mistake", [EOFError, KeyboardInterrupt])
def test_ending_the_consent_question_changes_nothing(host, monkeypatch, capsys, mistake):
    monkeypatch.setattr(cli, "on_terminal", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    def stop(_prompt=""):
        raise mistake

    monkeypatch.setattr("builtins.input", stop)
    assert cli.main(["setup", "--bridge-user", USER]) == 130
    assert not host.home.exists()


def test_without_a_terminal_setup_asks_nothing_and_names_the_flag(host, monkeypatch, capsys):
    answers(monkeypatch, on_terminal=False)
    assert cli.main(["setup"]) == 2
    assert "Pass --bridge-user" in capsys.readouterr().err


def test_yes_asks_nothing_and_names_the_flag(host, monkeypatch, capsys):
    answers(monkeypatch, on_terminal=True)
    assert cli.main(["setup", "--yes"]) == 2
    assert "Pass --bridge-user" in capsys.readouterr().err


def test_an_invalid_flag_username_is_refused_like_install(host, capsys):
    assert cli.main(["setup", "--dry-run", "--bridge-user", "has space"]) == 2
    assert "without spaces" in capsys.readouterr().err


# A store that cannot show what it holds: secret-tool's silent exit 1 ---------------


def unreadable(probe, *services):
    """secret-tool's answer for these services: no entry and a locked one look alike."""

    def has_secret(_store, service, _account, _locations=None):
        return None if service in services else True

    probe.has_secret = has_secret


def test_first_setup_on_libsecret_asks_instead_of_stopping(host, monkeypatch, capsys):
    unreadable(host.probe, BRIDGE_SERVICE, TOKEN_SERVICE)
    asked = answers(monkeypatch, "", "y", "n")
    assert cli.main(["setup", "--bridge-user", USER]) == 1
    assert asked[1].startswith("Store new ones for what it cannot show")
    out = capsys.readouterr().out
    assert "Prompt for the Bridge password" in out and "Generate the MCP bearer token" in out


def test_a_readable_token_is_kept_when_only_the_password_is_unreadable(host, monkeypatch, capsys):
    # A rerun after pressing Enter at the password prompt must not change the token.
    unreadable(host.probe, BRIDGE_SERVICE)
    answers(monkeypatch, "", "y", "n")
    assert cli.main(["setup", "--bridge-user", USER]) == 1
    out = capsys.readouterr().out
    assert "Prompt for the Bridge password" in out
    assert "Generate the MCP bearer token" not in out
    assert "The MCP bearer token is already in libsecret; keeping it" in out


@pytest.mark.parametrize("reply", ["", "n", "no", "anything"])
def test_declining_keeps_the_refusal_and_changes_nothing(host, monkeypatch, capsys, reply):
    unreadable(host.probe, BRIDGE_SERVICE, TOKEN_SERVICE)
    answers(monkeypatch, "", reply)
    assert cli.main(["setup", "--bridge-user", USER]) == 2
    assert "Cannot tell whether" in capsys.readouterr().err
    assert not host.home.exists()


@pytest.mark.parametrize(
    "argv", [["setup", "--bridge-user", USER], ["setup", "--yes", "--bridge-user", USER]]
)
def test_without_a_person_an_unreadable_store_still_stops(host, monkeypatch, capsys, argv):
    unreadable(host.probe, BRIDGE_SERVICE, TOKEN_SERVICE)
    answers(monkeypatch, on_terminal="--yes" in argv)
    assert cli.main(argv) == 2
    assert "--replace-credentials" in capsys.readouterr().err


def test_a_dry_run_with_an_unreadable_store_shows_the_plan_and_says_it_will_ask(host, capsys):
    unreadable(host.probe, BRIDGE_SERVICE, TOKEN_SERVICE)
    assert cli.main(["setup", "--dry-run", "--bridge-user", USER]) == 0
    out = capsys.readouterr().out
    assert "setup asks before storing new ones over them" in out
    assert "Prompt for the Bridge password" in out


def test_install_still_stops_on_an_unreadable_store(host, capsys):
    unreadable(host.probe, BRIDGE_SERVICE, TOKEN_SERVICE)
    assert cli.main(["install", "--dry-run", "--bridge-user", USER]) == 2
    assert "Cannot tell whether" in capsys.readouterr().err


# The password path, end to end on a fake host -----------------------------------------


@pytest.fixture
def applied(host, monkeypatch, tmp_path):
    """Run cli.main for real on the fake host: files go under tmp_path, every program,
    store call and network check is recorded, and nothing outside runs."""
    record = SimpleNamespace(commands=[], store_calls=[], checks=[], prompts=[], envs=[])
    # Bridge is installed, so no step downloads anything.
    host.probe.files["/usr/bin/protonmail-bridge"] = ""

    def runner(argv, **kwargs):
        record.commands.append(list(argv))
        record.envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def store_run(argv, **kwargs):
        record.store_calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, TOKEN if argv[1] == "lookup" else "", "")

    def prompt(label):
        record.prompts.append(label)
        return PASSWORD

    def check_service(port, token, say):
        record.checks.append((port, token))
        say("    ok: scripted")

    real = cli.Executor

    def factory(*, dry_run, **kwargs):
        return real(
            dry_run=dry_run,
            runner=runner,
            prompt=prompt,
            port_open=lambda _port: True,
            sleep=lambda _seconds: None,
            check_service=check_service,
            ask=lambda _prompt: pytest.fail("the login offer must not ask here"),
            **kwargs,
        )

    monkeypatch.setattr(cli, "Executor", factory)
    monkeypatch.setattr(credential_store.subprocess, "run", store_run)
    # Not the fake host's paths only: the executor writes real files.
    record.home = host.home
    return record


def attachments_line(host):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    return next(
        (
            line
            for line in Path(paths.env_file).read_text().splitlines()
            if line.startswith("PROTON_MCP_ATTACHMENTS_DIR=")
        ),
        None,
    )


def test_setup_shows_the_default_folder_and_leaves_it_to_the_first_save(host, applied, capsys):
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    folder = host.home / ".local/share/proton-mail-mcp/attachments"
    assert f"Attachments folder (PROTON_MCP_ATTACHMENTS_DIR): {folder}." in capsys.readouterr().out
    assert attachments_line(host) is None
    assert not folder.exists()


def test_setup_writes_and_creates_a_folder_given_by_flag(host, applied, capsys, tmp_path):
    folder = tmp_path / "chosen" / "files"
    argv = ["setup", "--yes", "--bridge-user", USER, "--attachments-dir", f"{folder}/"]
    assert cli.main(argv) == 0
    assert f"Attachments folder (PROTON_MCP_ATTACHMENTS_DIR): {folder}." in capsys.readouterr().out
    assert attachments_line(host) == f"PROTON_MCP_ATTACHMENTS_DIR={folder}"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_setup_writes_a_folder_typed_at_its_question_and_expands_home(host, applied, monkeypatch):
    asked = answers(monkeypatch, "relative/dir", "~/mail files", "y", "")
    assert cli.main(["setup", "--bridge-user", USER]) == 0
    folder = host.home / "mail files"
    assert "PROTON_MCP_ATTACHMENTS_DIR" in asked[0] and asked[1] == asked[0]
    assert attachments_line(host) == f"PROTON_MCP_ATTACHMENTS_DIR={folder}"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_setup_keeps_the_saved_folder_on_enter_and_shows_it(host, applied, monkeypatch, tmp_path):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    saved = tmp_path / "saved"
    host.probe.files[paths.env_file] = (
        f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\nPROTON_MCP_ATTACHMENTS_DIR={saved}\n"
    )
    asked = answers(monkeypatch, "", "y", "")
    assert cli.main(["setup"]) == 0
    assert f"[{saved}]" in asked[0]
    assert not saved.exists()


def test_setup_says_files_stay_behind_when_the_folder_changes(host, capsys, tmp_path):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    host.probe.files[paths.env_file] = (
        f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\nPROTON_MCP_ATTACHMENTS_DIR=/srv/old\n"
    )
    assert cli.main(["setup", "--dry-run", "--attachments-dir", str(tmp_path / "new")]) == 0
    assert "Files in the earlier folder are not moved." in capsys.readouterr().out


def test_a_folder_only_this_shell_finds_is_written_for_the_service(host, applied, capsys, tmp_path):
    # The service does not inherit XDG_DATA_HOME, so it would look under the home instead.
    host.probe.env["XDG_DATA_HOME"] = str(tmp_path / "data")
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    folder = tmp_path / "data" / "proton-mail-mcp" / "attachments"
    assert f"PROTON_MCP_ATTACHMENTS_DIR): {folder}." in capsys.readouterr().out
    assert attachments_line(host) == f"PROTON_MCP_ATTACHMENTS_DIR={folder}"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_a_folder_exported_in_the_shell_is_written_for_the_service(host, applied, tmp_path):
    folder = tmp_path / "exported"
    host.probe.env["PROTON_MCP_ATTACHMENTS_DIR"] = str(folder)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    assert attachments_line(host) == f"PROTON_MCP_ATTACHMENTS_DIR={folder}"


@pytest.mark.parametrize(
    "value",
    [
        "relative/dir",
        "/",
        "/.",
        "/srv/a\nb",
        # Line breaks only str.splitlines sees, and a byte that is not UTF-8.
        *(f"/srv/a{chr(code)}b" for code in (0x85, 0x2028, 0x2029, 0xDCFF)),
    ],
)
def test_an_unusable_folder_flag_names_the_setting_and_changes_nothing(host, capsys, value):
    assert cli.main(["setup", "--bridge-user", USER, "--attachments-dir", value]) == 2
    assert "PROTON_MCP_ATTACHMENTS_DIR" in capsys.readouterr().err
    assert not host.home.exists()


def test_three_unusable_answers_stop_before_any_plan(host, monkeypatch, capsys):
    answers(monkeypatch, "a", "b", "c")
    assert cli.main(["setup", "--bridge-user", USER]) == 2
    captured = capsys.readouterr()
    assert "--attachments-dir" in captured.err and "plan for" not in captured.out


def test_a_link_as_the_folder_refuses_at_setup(host, capsys, tmp_path):
    link = tmp_path / "link"
    host.probe.links[str(link)] = str(tmp_path)
    assert (
        cli.main(["setup", "--dry-run", "--bridge-user", USER, "--attachments-dir", str(link)]) == 2
    )
    assert "must not be a symbolic link" in capsys.readouterr().err


@pytest.mark.parametrize("below", ["", "below"], ids=["a-file", "under-a-file"])
def test_a_folder_that_cannot_be_created_leaves_a_first_install_unwritten(
    host, applied, capsys, tmp_path, below
):
    (tmp_path / "file").write_text("x")
    argv = ["--attachments-dir", str(tmp_path / "file" / below)]
    assert cli.main(["setup", "--yes", "--bridge-user", USER, *argv]) == 1
    assert "Cannot create" in capsys.readouterr().err
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    assert not Path(paths.env_file).exists()


def test_a_folder_that_cannot_be_created_leaves_the_settings_and_a_rerun_clean(
    host, applied, capsys, tmp_path
):
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    before = Path(paths.env_file).read_bytes()
    # The fake probe does not read the real file back.
    host.probe.files[paths.env_file] = before.decode()
    (tmp_path / "file").write_text("x")
    argv = ["setup", "--yes", "--attachments-dir", str(tmp_path / "file")]
    assert cli.main(argv) == 1
    assert Path(paths.env_file).read_bytes() == before
    # Nothing remembers the path, so a rerun cannot report success with it in place.
    assert cli.main(["setup", "--yes"]) == 0
    assert attachments_line(host) is None


def test_a_folder_that_cannot_be_created_fails_the_step_not_the_program(tmp_path):
    (tmp_path / "file").write_text("x")
    step = EnsureDir(str(tmp_path / "file" / "below"), "Create the private attachments folder")
    with pytest.raises(InstallError, match="Cannot create"):
        Executor(dry_run=False).run([step])


def test_the_password_reaches_the_store_on_stdin_only(applied, capsys):
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    captured = capsys.readouterr()
    stored = [call for call in applied.store_calls if call[0][1] == "store"]
    assert [call[0][-3] for call in stored] == [BRIDGE_SERVICE, TOKEN_SERVICE]
    assert stored[0][1]["input"] == PASSWORD
    # Never argv, never env, never output.
    for argv, kwargs in applied.store_calls:
        assert PASSWORD not in " ".join(argv)
        assert not any(PASSWORD in str(value) for value in (kwargs.get("env") or {}).values())
    for argv in applied.commands:
        assert PASSWORD not in " ".join(argv)
    assert not any(PASSWORD in str(env) for env in applied.envs if env)
    assert PASSWORD not in captured.out + captured.err
    assert applied.prompts[0].startswith(f"Bridge IMAP password for {USER}")
    assert len(applied.prompts) == 2


def test_no_secret_reaches_a_file_setup_wrote(applied, capsys):
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    generated = next(call[1]["input"] for call in applied.store_calls if TOKEN_SERVICE in call[0])
    written = [path for path in applied.home.rglob("*") if path.is_file()]
    assert {path.name for path in written} >= {
        "proton-mail-mcp.env",
        "proton-mail-mcp.service",
        "proton-mail-mcp-bridge.service",
    }
    for path in written:
        data = path.read_bytes()
        assert PASSWORD.encode() not in data, path
        assert generated.encode() not in data, path
    env_file = next(path for path in written if path.name == "proton-mail-mcp.env")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    captured = capsys.readouterr()
    assert generated not in captured.out + captured.err


def test_the_self_check_runs_last_with_the_stored_token(applied, capsys):
    assert cli.main(["setup", "--yes", "--bridge-user", USER, "--port", "9001"]) == 0
    captured = capsys.readouterr()
    assert applied.checks == [(9001, TOKEN)]
    assert "ok: scripted" in captured.out
    assert TOKEN not in captured.out + captured.err
    assert captured.out.rstrip().endswith("setup finished.")


def test_a_failed_self_check_fails_setup_and_says_where_to_look(applied, monkeypatch, capsys):
    real = cli.Executor

    def failing(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)

        def check_service(_port, _token, _say):
            raise selfcheck.SelfCheckError("the named check failed")

        executor.check_service = check_service
        return executor

    monkeypatch.setattr(cli, "Executor", failing)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 1
    captured = capsys.readouterr()
    assert "the named check failed" in captured.err
    assert "journalctl --user -u proton-mail-mcp.service" in captured.err
    assert "setup finished" not in captured.out
    assert PASSWORD not in captured.out + captured.err


def test_yes_never_opens_bridges_cli_even_on_a_terminal(applied, monkeypatch, capsys):
    real = cli.Executor

    def free_port_on_a_terminal(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)
        executor.port_open = lambda _port: False
        executor.terminal = lambda: True
        return executor

    monkeypatch.setattr(cli, "Executor", free_port_on_a_terminal)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    assert "not opened: it needs a person" in capsys.readouterr().out
    assert not any("--cli" in argv for argv in applied.commands)


def test_no_terminal_for_the_password_stops_before_anything_changes(host, monkeypatch, capsys):
    monkeypatch.setattr(cli, "terminal_available", lambda: False)
    no_real_run(monkeypatch)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 2
    captured = capsys.readouterr()
    assert "can only be typed at a terminal" in captured.err
    assert "plan for" not in captured.out
    assert not host.home.exists()


def test_an_agent_without_a_terminal_can_install_all_but_the_password(applied, monkeypatch, capsys):
    monkeypatch.setattr(cli, "terminal_available", lambda: False)
    argv = ["setup", "--yes", "--skip-credentials", "--bridge-user", USER]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert applied.prompts == []
    assert applied.checks == []
    assert "Self-check skipped (--skip-credentials)" in captured.out


def test_no_terminal_is_fine_when_the_credentials_are_already_stored(applied, monkeypatch, capsys):
    monkeypatch.setattr(cli, "terminal_available", lambda: False)
    host_probe = cli.HostProbe(None)
    host_probe.secrets = {("libsecret", BRIDGE_SERVICE, USER), ("libsecret", TOKEN_SERVICE, USER)}
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    assert applied.prompts == []
    assert len(applied.checks) == 1


def test_a_terminal_that_cannot_hide_input_refuses_to_read_the_password(
    applied, monkeypatch, capsys
):
    real = cli.Executor

    def unhideable(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)

        def prompt(_label):
            import warnings

            warnings.warn("cannot hide", getpass.GetPassWarning, stacklevel=1)
            return PASSWORD

        executor.prompt = prompt
        return executor

    monkeypatch.setattr(cli, "Executor", unhideable)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 1
    captured = capsys.readouterr()
    assert "cannot hide the password" in captured.err
    assert not [call for call in applied.store_calls if call[0][1] == "store"]


def test_entries_that_differ_store_nothing(applied, monkeypatch, capsys):
    real = cli.Executor
    typed = iter(["first", "second"])

    def mistyped(*, dry_run, **kwargs):
        executor = real(dry_run=dry_run, **kwargs)
        executor.prompt = lambda _label: next(typed)
        return executor

    monkeypatch.setattr(cli, "Executor", mistyped)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 1
    assert "the two entries differ" in capsys.readouterr().err
    assert not [call for call in applied.store_calls if call[0][1] == "store"]


def test_keychain_prompts_by_itself_so_the_password_never_passes_through(monkeypatch, tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    executor = Executor(
        dry_run=False,
        out=io.StringIO(),
        runner=runner,
        prompt=lambda _label: pytest.fail("the installer must not read this password"),
    )
    executor.run([StoreSecret("keychain", BRIDGE_SERVICE, USER, "prompt", "Save it")])
    assert calls[0][0][-1] == "-w" and calls[0][0][0] == credential_store.SECURITY
    assert "input" not in calls[0][1]


# The Bridge login offer -------------------------------------------------------------


LOGIN = BridgeLogin(("/opt/bridge", "--cli"), "Offer Bridge's CLI to log in", 1143)


def login_executor(*, port_open=False, terminal=True, assume_yes=False, reply="", run_result=0):
    out = io.StringIO()
    record = SimpleNamespace(out=out, questions=[], runs=[])

    def runner(argv, **kwargs):
        record.runs.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, run_result)

    def ask(question):
        record.questions.append(question)
        return reply

    executor = Executor(
        dry_run=False,
        out=out,
        runner=runner,
        port_open=lambda _port: port_open,
        terminal=lambda: terminal,
        assume_yes=assume_yes,
        ask=ask,
    )
    return executor, record


def test_login_opens_bridges_cli_on_this_terminal_after_asking():
    executor, record = login_executor()
    executor.run([LOGIN])
    assert record.questions and "Open Bridge's CLI" in record.questions[0]
    # Inherits the terminal: no pipes, so Bridge itself reads the Proton password.
    assert record.runs == [(["/opt/bridge", "--cli"], {"check": False})]
    assert "login" in record.out.getvalue() and "exit" in record.out.getvalue()


@pytest.mark.parametrize("reply", ["n", "N", "no", " no "])
def test_login_declined_runs_nothing(reply):
    executor, record = login_executor(reply=reply)
    executor.run([LOGIN])
    assert record.runs == []
    assert executor.warnings == []


@pytest.mark.parametrize(
    "settings", [{"port_open": True}, {"terminal": False}, {"assume_yes": True}]
)
def test_login_is_not_offered_when_it_cannot_or_must_not_ask(settings):
    executor, record = login_executor(**settings)
    executor.run([LOGIN])
    assert record.questions == [] and record.runs == []
    assert "not opened" in record.out.getvalue()


def test_login_a_dry_run_only_shows_the_command():
    out = io.StringIO()
    executor = Executor(
        dry_run=True,
        out=out,
        runner=lambda *a, **k: pytest.fail("ran"),
        port_open=lambda _p: pytest.fail("probed"),
        ask=lambda _q: pytest.fail("asked"),
    )
    executor.run([LOGIN])
    assert "$ /opt/bridge --cli" in out.getvalue()


def test_login_a_failing_cli_only_warns():
    executor, _record = login_executor(run_result=3)
    executor.run([LOGIN])
    assert executor.warnings == ["Bridge's CLI exited with status 3"]


def test_login_a_missing_cli_only_warns():
    executor, _record = login_executor()

    def missing(_argv, **_kwargs):
        raise FileNotFoundError

    executor.runner = missing
    executor.run([LOGIN])
    assert "cannot run /opt/bridge" in executor.warnings[0]


# The login offer on a rerun, with the installer's own Bridge service --------------------


CLI = ["/opt/bridge", "--cli"]
STATE = ["systemctl", "--user", "show", "--property=ActiveState", "--value", "bridge.service"]
STOP = ["systemctl", "--user", "stop", "bridge.service"]
START = ["systemctl", "--user", "start", "bridge.service"]
PGREP = ["pgrep", "-u", "1000", "-f", "bridge --noninteractive"]
BRIDGE_UNIT = BridgeService(
    CheckStopped(tuple(STATE), "Check it", "", states=("inactive", "failed")),
    tuple(STOP),
    tuple(START),
)
LOGIN_AROUND = BridgeLogin(
    tuple(CLI),
    "Offer Bridge's CLI to log in",
    1143,
    service=BRIDGE_UNIT,
    processes=tuple(PGREP),
)
LOGIN_WITH_PASSWORD = dataclasses.replace(LOGIN_AROUND, password_stored=True)


class ScriptedHost:
    """Plays the service manager, pgrep, and Bridge's CLI; records every command. The
    service is a state: stop and start change what the query answers, and the port and
    the process list follow it, unless `other_bridge` keeps them busy."""

    def __init__(
        self,
        *,
        running=True,
        listening=True,
        other_bridge=False,
        stop_status=0,
        stop_works=True,
        start_status=(0,),
        query_status=0,
        cli_status=0,
        pgrep_status=None,
        reply="",
        terminal=True,
        assume_yes=False,
    ):
        self.running = running
        self.listening = listening
        self.other_bridge = other_bridge
        self.stop_status = stop_status
        self.stop_works = stop_works
        self.start_status = list(start_status)
        self.query_status = query_status
        self.cli_status = cli_status
        self.pgrep_status = pgrep_status
        self.calls = []
        self.questions = []
        self.pauses = []
        self.cli_beside_a_bridge = False
        self.out = io.StringIO()

        def ask(question):
            self.questions.append(question)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        self.executor = Executor(
            dry_run=False,
            out=self.out,
            runner=self.run,
            port_open=self.port_open,
            sleep=self.pauses.append,
            terminal=lambda: terminal,
            assume_yes=assume_yes,
            ask=ask,
        )

    def port_open(self, port):
        assert port == 1143
        return self.other_bridge or (self.running and self.listening)

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv == STATE:
            state = "active" if self.running else "inactive"
            return subprocess.CompletedProcess(argv, self.query_status, f"{state}\n", "")
        if argv == STOP:
            if self.stop_works:
                self.running = False
            return subprocess.CompletedProcess(argv, self.stop_status)
        if argv == START:
            status = self.start_status.pop(0)
            if status == 0:
                self.running = True
            return subprocess.CompletedProcess(argv, status)
        if argv == PGREP:
            if self.pgrep_status is not None:
                return subprocess.CompletedProcess(argv, self.pgrep_status)
            busy = self.running or self.other_bridge
            return subprocess.CompletedProcess(argv, 0 if busy else 1)
        assert argv == CLI, argv
        # Inherits the terminal: no pipes, so Bridge itself reads the Proton password.
        assert kwargs == {"check": False}
        self.cli_beside_a_bridge = self.running or self.other_bridge
        return subprocess.CompletedProcess(argv, self.cli_status)

    def changes(self):
        return [call for call in self.calls if call in (STOP, START, CLI)]

    def said(self):
        return self.out.getvalue()


def test_no_stored_password_means_enter_stops_the_service_for_the_cli():
    # The finding: a first setup's login was declined, the service runs, and every rerun
    # failed the same way. Bridge listens with no account too, so the port is no guide.
    host = ScriptedHost()
    assert host.port_open(1143) is True
    host.executor.run([LOGIN_AROUND])
    assert "No Bridge password is stored yet" in host.questions[0]
    assert host.questions[0].rstrip().endswith("[Y/n]")
    assert host.changes() == [STOP, CLI, START]
    assert host.cli_beside_a_bridge is False
    assert host.running is True
    assert host.executor.warnings == []
    assert "`login`" in host.said() and "$ systemctl --user stop" in host.said()
    assert "$ systemctl --user start" in host.said()


def test_the_cli_opens_only_after_the_stop_is_proven():
    host = ScriptedHost()
    host.executor.run([LOGIN_AROUND])
    assert host.calls.index(STOP) < host.calls.index(CLI) < host.calls.index(START)
    after_stop = host.calls[host.calls.index(STOP) + 1 : host.calls.index(CLI)]
    assert after_stop == [STATE, PGREP]


@pytest.mark.parametrize("reply", ["y", "Y", "yes", " YES "])
def test_a_stored_password_still_lets_a_person_log_in(reply):
    host = ScriptedHost(reply=reply)
    host.executor.run([LOGIN_WITH_PASSWORD])
    assert "A Bridge password is stored" in host.questions[0]
    assert "`info`" in host.questions[0] and host.questions[0].rstrip().endswith("[y/N]")
    assert host.changes() == [STOP, CLI, START]
    assert host.cli_beside_a_bridge is False


@pytest.mark.parametrize("reply", ["", "n", "no", "anything"])
def test_a_stored_password_means_enter_stops_nothing(reply):
    # A healthy rerun stays healthy unless someone wants another login.
    host = ScriptedHost(reply=reply)
    host.executor.run([LOGIN_WITH_PASSWORD])
    assert host.changes() == []
    assert host.running is True and host.executor.warnings == []


@pytest.mark.parametrize("reply", ["n", "N", "no", " no "])
def test_declining_with_no_stored_password_stops_nothing(reply):
    host = ScriptedHost(reply=reply)
    host.executor.run([LOGIN_AROUND])
    assert host.changes() == []
    assert host.running is True and host.executor.warnings == []


@pytest.mark.parametrize("listening", [True, False])
@pytest.mark.parametrize(
    ("login", "default"), [(LOGIN_AROUND, "[Y/n]"), (LOGIN_WITH_PASSWORD, "[y/N]")]
)
def test_the_port_does_not_change_the_question(login, default, listening):
    host = ScriptedHost(listening=listening, reply="n")
    host.executor.run([login])
    assert host.questions[0].rstrip().endswith(default)


@pytest.mark.parametrize("settings", [{"terminal": False}, {"assume_yes": True}])
@pytest.mark.parametrize("login", [LOGIN_AROUND, LOGIN_WITH_PASSWORD])
def test_without_a_person_nothing_is_stopped_or_even_asked(settings, login):
    host = ScriptedHost(**settings)
    host.executor.run([login])
    assert host.calls == [] and host.questions == []
    assert "not opened: it needs a person" in host.said()


def test_an_interrupted_question_stops_nothing():
    host = ScriptedHost(listening=False, reply=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        host.executor.run([LOGIN_AROUND])
    assert host.changes() == []


def test_a_service_that_did_not_stop_never_gets_the_cli_beside_it():
    host = ScriptedHost(listening=False, stop_works=False)
    host.executor.run([LOGIN_AROUND])
    assert CLI not in host.calls
    assert host.executor.warnings == [
        "Bridge's CLI was not opened: the installer's Bridge service did not stop"
    ]
    assert host.changes() == [STOP, START]


def test_a_stop_command_that_fails_but_stopped_it_is_fine():
    # What counts is the query afterwards, not what the stop answered.
    host = ScriptedHost(listening=False, stop_status=1)
    host.executor.run([LOGIN_AROUND])
    assert host.changes() == [STOP, CLI, START]


@pytest.mark.parametrize(
    "failure", [OSError("no systemctl"), subprocess.TimeoutExpired("systemctl", 120)]
)
def test_a_stop_command_that_cannot_run_never_gets_the_cli_beside_it(failure):
    host = ScriptedHost(listening=False)
    real = host.run

    def run(argv, **kwargs):
        if list(argv) == STOP:
            host.calls.append(list(argv))
            raise failure
        return real(argv, **kwargs)

    host.executor.runner = run
    host.executor.run([LOGIN_AROUND])
    assert CLI not in host.calls
    assert "did not stop" in host.executor.warnings[0]


def test_a_query_that_cannot_answer_counts_as_running_and_gets_no_cli():
    # A failed query never proves stopped: first it reads as running, then as not stopped.
    host = ScriptedHost(running=False, listening=False, query_status=1)
    host.executor.run([LOGIN_AROUND])
    assert host.questions, "the offer is still made"
    assert CLI not in host.calls


def test_another_bridge_that_holds_the_port_after_the_stop_gets_no_cli():
    host = ScriptedHost(other_bridge=True)
    host.executor.run([LOGIN_AROUND])
    assert CLI not in host.calls
    assert "127.0.0.1:1143 is in use" in host.executor.warnings[0]
    assert host.running is True, "the installer's own service comes back"
    assert host.pauses == [0.5] * (execute._BRIDGE_STOP_CHECKS - 1)


def test_another_bridge_process_seen_after_the_stop_gets_no_cli():
    # Not listening: only the process list can show it.
    host = ScriptedHost(listening=False, pgrep_status=0)
    host.executor.run([LOGIN_AROUND])
    assert CLI not in host.calls
    assert "a Bridge is running" in host.executor.warnings[0]
    assert host.changes() == [STOP, START]


def test_a_slow_exit_is_waited_for_before_the_cli():
    host = ScriptedHost(listening=False)
    real = host.run
    busy_answers = [0, 0, 1]

    def run(argv, **kwargs):
        if list(argv) == PGREP:
            host.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, busy_answers.pop(0))
        return real(argv, **kwargs)

    host.executor.runner = run
    host.executor.run([LOGIN_AROUND])
    assert host.changes() == [STOP, CLI, START]
    assert host.pauses == [0.5, 0.5]


@pytest.mark.parametrize(
    ("cli", "warning"),
    [
        (3, "Bridge's CLI exited with status 3"),
        (FileNotFoundError(), "cannot run /opt/bridge; log in to Bridge by hand"),
    ],
)
def test_a_failing_cli_still_starts_the_service_again(cli, warning):
    host = ScriptedHost(listening=False)
    real = host.run

    def run(argv, **kwargs):
        if list(argv) == CLI:
            host.calls.append(list(argv))
            if isinstance(cli, Exception):
                raise cli
            return subprocess.CompletedProcess(argv, cli)
        return real(argv, **kwargs)

    host.executor.runner = run
    host.executor.run([LOGIN_AROUND])
    assert host.executor.warnings == [warning]
    assert host.changes() == [STOP, CLI, START]
    assert host.running is True


def test_ctrl_c_in_the_cli_still_starts_the_service_again():
    host = ScriptedHost(listening=False)
    real = host.run

    def run(argv, **kwargs):
        if list(argv) == CLI:
            host.calls.append(list(argv))
            raise KeyboardInterrupt
        return real(argv, **kwargs)

    host.executor.runner = run
    with pytest.raises(KeyboardInterrupt):
        host.executor.run([LOGIN_AROUND])
    assert host.changes() == [STOP, CLI, START]
    assert host.running is True


def test_a_start_refused_for_a_moment_is_retried():
    # launchctl refuses a bootstrap for a moment after a bootout.
    host = ScriptedHost(listening=False, start_status=(5, 5, 0))
    host.executor.run([LOGIN_AROUND])
    assert host.calls.count(START) == 3
    assert host.pauses == [1.0, 1.0]
    assert host.executor.warnings == []


def test_a_start_that_keeps_failing_only_warns_and_says_how():
    host = ScriptedHost(listening=False, start_status=(5, 5, 5, 5))
    host.executor.run([LOGIN_AROUND])
    assert host.calls.count(START) == 4
    assert host.executor.warnings == [
        "cannot start Bridge's service again; run `systemctl --user start bridge.service`"
    ]


def test_a_start_that_cannot_run_only_warns():
    host = ScriptedHost(listening=False)
    real = host.run

    def run(argv, **kwargs):
        if list(argv) == START:
            host.calls.append(list(argv))
            raise OSError("no systemctl")
        return real(argv, **kwargs)

    host.executor.runner = run
    host.executor.run([LOGIN_AROUND])
    assert host.calls.count(START) == 4
    assert "cannot start Bridge's service again" in host.executor.warnings[0]


def test_a_service_that_never_stopped_refusing_a_start_needs_no_warning():
    # A second bootstrap of a loaded agent fails; the service is simply still there.
    host = ScriptedHost(listening=False, stop_works=False, start_status=(5, 5, 5, 5))
    host.executor.run([LOGIN_AROUND])
    assert host.executor.warnings == [
        "Bridge's CLI was not opened: the installer's Bridge service did not stop"
    ]


def test_a_stopped_service_gets_the_plain_offer_and_is_not_started_by_it():
    # Starting it stays with the plan's own later step, as on a first setup.
    host = ScriptedHost(running=False, listening=False)
    host.executor.run([LOGIN_AROUND])
    assert "Open Bridge's CLI here" in host.questions[0]
    assert host.changes() == [CLI]
    assert host.calls[:2] == [STATE, PGREP]


def test_a_stopped_service_with_something_on_the_port_gets_no_cli():
    host = ScriptedHost(running=False, other_bridge=True)
    host.executor.run([LOGIN_AROUND])
    assert host.questions == [] and host.changes() == []
    assert "not opened: 127.0.0.1:1143 is in use" in host.said()


@pytest.mark.parametrize(
    "login", [LOGIN_AROUND, BridgeLogin(tuple(CLI), "Offer it", 1143, processes=tuple(PGREP))]
)
def test_a_noninteractive_bridge_that_does_not_listen_gets_no_cli(login):
    # The finding's second half: a Bridge still starting up holds no port, so a free port
    # proves nothing.
    host = ScriptedHost(running=False, listening=False, pgrep_status=0)
    host.executor.run([login])
    assert host.questions == [] and host.changes() == []
    assert "not opened: a Bridge is running" in host.said()


@pytest.mark.parametrize(
    "status_or_failure",
    [2, 3, FileNotFoundError(), subprocess.TimeoutExpired("pgrep", 60)],
)
def test_a_process_check_that_proves_nothing_gets_no_cli(status_or_failure):
    host = ScriptedHost(running=False, listening=False)
    real = host.run

    def run(argv, **kwargs):
        if list(argv) == PGREP:
            if isinstance(status_or_failure, Exception):
                raise status_or_failure
            return subprocess.CompletedProcess(argv, status_or_failure)
        return real(argv, **kwargs)

    host.executor.runner = run
    host.executor.run([LOGIN_AROUND])
    assert host.questions == [] and host.changes() == []
    assert "not opened: cannot check whether a Bridge is running" in host.said()


def test_a_dry_run_shows_the_stop_and_start_and_runs_nothing():
    out = io.StringIO()
    executor = Executor(
        dry_run=True,
        out=out,
        runner=lambda *a, **k: pytest.fail("ran"),
        port_open=lambda _p: pytest.fail("probed"),
        ask=lambda _q: pytest.fail("asked"),
    )
    executor.run([LOGIN_AROUND])
    text = out.getvalue()
    assert "$ /opt/bridge --cli" in text
    assert "$ systemctl --user stop bridge.service" in text
    assert "$ systemctl --user start bridge.service" in text


# The login offer in the plan ----------------------------------------------------------


def plan_login(family="debian", probe=None, **overrides):
    return kinds(plan(family, probe, **overrides), BridgeLogin)[0]


def installer_unit(probe):
    paths = paths_for(simulated("debian"), probe.env, probe.home)
    probe.files[f"{paths.unit_dir}/{templates.BRIDGE_UNIT}"] = f"# {MARKER}\n"
    return paths


def test_a_first_setup_has_no_service_to_stop_but_still_checks_for_a_bridge():
    login = plan_login()
    assert login.service is None
    assert login.processes == ("pgrep", "-u", "1000", "-f", BRIDGE_PROCESSES)
    assert login.password_stored is False


def test_the_login_offer_knows_a_stored_password():
    probe = simulated_probe(simulated("debian"))
    probe.secrets = {("libsecret", BRIDGE_SERVICE, USER)}
    assert plan_login(probe=probe).password_stored is True
    # The token alone says nothing about Bridge.
    probe.secrets = {("libsecret", TOKEN_SERVICE, USER)}
    assert plan_login(probe=probe).password_stored is False


def test_a_store_that_cannot_say_counts_as_no_password():
    probe = simulated_probe(simulated("debian"))
    probe.has_secret = lambda *_args, **_kwargs: None
    assert plan_login(probe=probe, store_unreadable=True).password_stored is False


@pytest.mark.parametrize("flag", ["replace_credentials", "skip_credentials"])
def test_a_run_that_leaves_the_stored_password_aside_does_not_ask_about_it(flag):
    # A dry run without a username relies on this: it asks no store about the placeholder.
    probe = simulated_probe(simulated("debian"))
    probe.secrets = {("libsecret", BRIDGE_SERVICE, USER)}
    assert plan_login(probe=probe, **{flag: True}).password_stored is False
    assert probe.secret_locations == []


def test_linux_login_uses_the_installers_unit():
    probe = simulated_probe(simulated("debian"))
    installer_unit(probe)
    service = plan_login(probe=probe).service
    unit = templates.BRIDGE_UNIT
    assert service.stop == ("systemctl", "--user", "stop", unit)
    assert service.start == ("systemctl", "--user", "start", unit)
    assert service.stopped.argv == (
        "systemctl",
        "--user",
        "show",
        "--property=ActiveState",
        "--value",
        unit,
    )
    assert service.stopped.states == ("inactive", "failed")


def test_macos_login_uses_the_installers_launchagent():
    probe = simulated_probe(simulated("macos"))
    paths = paths_for(simulated("macos"), probe.env, probe.home)
    plist = f"{paths.agents_dir}/{templates.BRIDGE_LABEL}.plist"
    probe.files[plist] = f"<!-- {MARKER} -->"
    target = f"gui/501/{templates.BRIDGE_LABEL}"
    probe.outputs[("launchctl", "print", target)] = f"    path = {plist}\n"
    login = plan_login("macos", probe)
    service = login.service
    assert service.stop == ("launchctl", "bootout", target)
    assert service.start == ("launchctl", "bootstrap", "gui/501", plist)
    assert service.stopped.argv == ("launchctl", "print", target)
    assert service.stopped.status == 113
    assert login.processes == ("pgrep", "-u", "501", "-f", BRIDGE_PROCESSES)


def test_a_launchagent_loaded_from_another_file_is_never_the_installers_to_stop():
    probe = simulated_probe(simulated("macos"))
    paths = paths_for(simulated("macos"), probe.env, probe.home)
    plist = f"{paths.agents_dir}/{templates.BRIDGE_LABEL}.plist"
    probe.files[plist] = f"<!-- {MARKER} -->"
    target = f"gui/501/{templates.BRIDGE_LABEL}"
    probe.outputs[("launchctl", "print", target)] = "    path = /elsewhere.plist\n"
    assert plan_login("macos", probe, skip_bridge_service=True).service is None


def test_a_unit_the_installer_did_not_write_is_never_stopped():
    probe = simulated_probe(simulated("debian"))
    paths = paths_for(simulated("debian"), probe.env, probe.home)
    probe.files[f"{paths.unit_dir}/{templates.BRIDGE_UNIT}"] = "[Service]\nExecStart=/x\n"
    assert plan_login(probe=probe).service is None


def test_a_unit_systemd_loads_from_elsewhere_is_never_stopped():
    probe = simulated_probe(simulated("debian"))
    installer_unit(probe)
    query = ("systemctl", "--user", "show", "--property=FragmentPath", "--value")
    probe.outputs[(*query, templates.BRIDGE_UNIT)] = "/etc/systemd/user/bridge.service\n"
    assert plan_login(probe=probe, skip_bridge_service=True).service is None


def test_a_host_without_a_user_manager_has_no_service_to_stop():
    probe = simulated_probe(simulated("fedora"))
    installer_unit(probe)
    del probe.files[SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    login = plan_login("fedora", probe)
    assert login.service is None
    assert login.processes


@pytest.mark.parametrize("flag", ["skip_bridge_service", "skip_services"])
def test_the_installers_running_unit_still_counts_with_a_skip_flag(flag):
    # Those flags are about what this run sets up; a unit an earlier run left still runs.
    probe = simulated_probe(simulated("debian"))
    installer_unit(probe)
    assert plan_login(probe=probe, **{flag: True}).service is not None


@pytest.mark.parametrize(
    "line",
    [
        "/usr/bin/protonmail-bridge --noninteractive",
        "/usr/bin/protonmail-bridge-core --noninteractive",
        "/usr/lib/protonmail/bridge/bridge --launcher /x/proton-bridge --noninteractive",
        "/Applications/Proton Mail Bridge.app/Contents/MacOS/Proton Mail Bridge --noninteractive",
        "/Applications/Proton Mail Bridge.app/Contents/MacOS/bridge --launcher /x --noninteractive",
        "protonmail-bridge --noninteractive",
        # A script: the kernel puts its interpreter first.
        "/usr/bin/python3 /opt/shims/fake-bridge --noninteractive",
    ],
)
def test_the_process_pattern_finds_a_noninteractive_bridge(line):
    assert re.search(BRIDGE_PROCESSES, line)


@pytest.mark.parametrize(
    "line",
    [
        "/usr/bin/protonmail-bridge --cli",
        "/usr/bin/protonmail-bridge",
        "/usr/lib/protonmail/bridge/bridge-gui",
        # Commands that only mention both, such as a prompt or a shell's script.
        "claude -p stop bridge --cli and any --noninteractive instance",
        "/bin/sh -c protonmail-bridge --noninteractive",
        "apt-get --noninteractive install bridge-utils",
        "/usr/bin/python3 setup.py --bridge-bin /opt/bridge --noninteractive-ish",
    ],
)
def test_the_process_pattern_ignores_what_only_mentions_it(line):
    assert not re.search(BRIDGE_PROCESSES, line)


def test_the_process_pattern_is_read_by_pgrep_as_a_posix_pattern():
    if shutil.which("pgrep") is None:
        pytest.skip("needs pgrep")
    result = REAL_RUN(
        ["pgrep", "-u", str(os.getuid()), "-f", f"{BRIDGE_PROCESSES}-no-such-process"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    # 1: read and matched nothing; 2 would be a pattern pgrep cannot read.
    assert result.returncode == 1


# The login offer through the whole command ---------------------------------------------


@pytest.fixture
def rerun(host, monkeypatch):
    """A rerun on a terminal on the fake host: the units of a first setup are there,
    Bridge's service runs and listens, and every program is scripted."""
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    for unit in (templates.BRIDGE_UNIT, templates.MCP_UNIT):
        host.probe.files[f"{paths.unit_dir}/{unit}"] = f"# {MARKER}\n"
    host.probe.files["/usr/bin/protonmail-bridge"] = ""
    host.probe.secrets = {("libsecret", TOKEN_SERVICE, USER)}
    record = SimpleNamespace(commands=[], bridge_running=True, checks=[], stored=[])
    cli_argv = ["/usr/bin/protonmail-bridge", "--cli"]
    unit = templates.BRIDGE_UNIT

    def runner(argv, **_kwargs):
        argv = list(argv)
        record.commands.append(argv)
        if argv == ["systemctl", "--user", "show", "--property=ActiveState", "--value", unit]:
            state = "active" if record.bridge_running else "inactive"
            return subprocess.CompletedProcess(argv, 0, f"{state}\n", "")
        if argv == ["systemctl", "--user", "stop", unit]:
            record.bridge_running = False
        elif argv == ["systemctl", "--user", "start", unit]:
            record.bridge_running = True
        elif argv[0] == "pgrep":
            return subprocess.CompletedProcess(argv, 0 if record.bridge_running else 1, "", "")
        elif argv == cli_argv:
            assert not record.bridge_running, "Bridge's CLI opened beside a running Bridge"
        return subprocess.CompletedProcess(argv, 0, "", "")

    def store_run(argv, **kwargs):
        if argv[1] == "store":
            record.stored.append((argv[-3], kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, TOKEN if argv[1] == "lookup" else "", "")

    real = cli.Executor

    def factory(*, dry_run, **kwargs):
        return real(
            dry_run=dry_run,
            runner=runner,
            prompt=lambda _label: PASSWORD,
            # Bridge listens with no account logged in too.
            port_open=lambda port: port == 8765 or record.bridge_running,
            sleep=lambda _seconds: None,
            check_service=lambda port, _token, _say: record.checks.append(port),
            terminal=lambda: True,
            **kwargs,
        )

    monkeypatch.setattr(cli, "Executor", factory)
    monkeypatch.setattr(credential_store.subprocess, "run", store_run)
    record.cli_argv = cli_argv
    record.stop = ["systemctl", "--user", "stop", unit]
    record.start = ["systemctl", "--user", "start", unit]
    record.host = host
    return record


def test_a_rerun_with_no_password_logs_in_around_the_service_then_stores_it(
    rerun, monkeypatch, capsys
):
    # Enter at the offer, as the finding's person would after a declined first login.
    asked = answers(monkeypatch, "", "y", "")
    assert cli.main(["setup", "--bridge-user", USER]) == 0
    assert "No Bridge password is stored yet" in asked[2]
    order = [rerun.commands.index(argv) for argv in (rerun.stop, rerun.cli_argv, rerun.start)]
    assert order == sorted(order)
    assert rerun.bridge_running is True
    assert rerun.stored == [(BRIDGE_SERVICE, PASSWORD)]
    assert rerun.checks == [8765]
    captured = capsys.readouterr()
    assert "setup finished." in captured.out
    assert PASSWORD not in captured.out + captured.err


def test_a_healthy_rerun_is_left_alone_on_enter(rerun, monkeypatch, capsys):
    rerun.host.probe.secrets.add(("libsecret", BRIDGE_SERVICE, USER))
    asked = answers(monkeypatch, "", "y", "")
    assert cli.main(["setup", "--bridge-user", USER]) == 0
    assert "A Bridge password is stored" in asked[2]
    assert rerun.stop not in rerun.commands and rerun.cli_argv not in rerun.commands
    assert rerun.checks == [8765]
    assert "setup finished." in capsys.readouterr().out


def test_a_rerun_with_yes_never_stops_the_service(rerun, monkeypatch, capsys):
    rerun.host.probe.secrets.add(("libsecret", BRIDGE_SERVICE, USER))
    answers(monkeypatch, on_terminal=True)
    assert cli.main(["setup", "--yes", "--bridge-user", USER]) == 0
    assert rerun.stop not in rerun.commands and rerun.cli_argv not in rerun.commands
    assert "not opened: it needs a person" in capsys.readouterr().out


def test_a_dry_run_of_a_rerun_shows_the_way_and_changes_nothing(host, monkeypatch, capsys):
    paths = paths_for(host.platform, host.probe.env, host.probe.home)
    host.probe.files[f"{paths.unit_dir}/{templates.BRIDGE_UNIT}"] = f"# {MARKER}\n"
    no_real_run(monkeypatch)
    assert cli.main(["setup", "--dry-run", "--bridge-user", USER]) == 0
    out = capsys.readouterr().out
    assert f"$ systemctl --user stop {templates.BRIDGE_UNIT}" in out
    assert f"$ systemctl --user start {templates.BRIDGE_UNIT}" in out


# The self-check step and its client -------------------------------------------------


STEP = SelfCheck(8765, "libsecret", TOKEN_SERVICE, USER, "Check it", hint="the logs")


def test_self_check_step_a_dry_run_reads_no_token(monkeypatch):
    monkeypatch.setattr(credential_store, "lookup", lambda *a: pytest.fail("read the token"))
    out = io.StringIO()
    executor = Executor(dry_run=True, out=out, check_service=lambda *a: pytest.fail("connected"))
    executor.run([STEP])
    assert "count_mail" in out.getvalue() and "never printed" in out.getvalue()


def test_self_check_step_a_store_that_fails_stops_with_its_hint(monkeypatch):
    def lookup(*_args):
        raise credential_store.CredentialStoreError("Credential is unavailable")

    monkeypatch.setattr(credential_store, "lookup", lookup)
    executor = Executor(dry_run=False, out=io.StringIO())
    with pytest.raises(InstallError, match="Credential is unavailable; see the logs"):
        executor.run([STEP])


class Reply:
    """A urllib response: status, headers, read() and line iteration."""

    def __init__(self, status=200, body=b"", kind="application/json", session=None):
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = kind
        if session:
            self.headers["Mcp-Session-Id"] = session
        self.stream = io.BytesIO(body)
        self.read = self.stream.read

    def __iter__(self):
        return iter(self.stream)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def refused(status=401):
    return urllib.error.HTTPError("http://x", status, "no", email.message.Message(), io.BytesIO())


def sse(payload):
    # Server keep-alive comment first; the reply is not the first line.
    data = b": ping\n\nevent: message\ndata: " + json.dumps(payload).encode() + b"\n\n"
    return Reply(body=data, kind="text/event-stream", session=None)


def initialized(request_id=2, name=selfcheck.SERVER_NAME):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": name}},
    }


def tool_ok(request_id, **fields):
    result = {"folder": "INBOX", "count": 3, **fields}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False},
    }


class Script:
    """Answers each request from a list; records what was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        assert timeout > 0
        assert self.replies, f"unexpected request {request.data}"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def bodies(self):
        return [json.loads(request.data) for request in self.requests]


def happy_script():
    init = sse(initialized(1))
    init.headers["Mcp-Session-Id"] = "session-1"
    return Script(refused(401), init, Reply(202), sse(tool_ok(2)))


def run_check(script):
    said = []
    selfcheck.check_service(8765, TOKEN, said.append, open_url=script, sleep=lambda _seconds: None)
    return said


def test_client_proves_auth_then_mail_and_shows_no_mail_data():
    script = happy_script()
    said = run_check(script)
    methods = [body["method"] for body in script.bodies()]
    assert methods == ["initialize", "initialize", "notifications/initialized", "tools/call"]
    first, second, _note, tool = script.requests
    assert first.get_header("Authorization") is None
    assert second.get_header("Authorization") == f"Bearer {TOKEN}"
    assert tool.get_header("Mcp-session-id") == "session-1"
    assert tool.get_header("Mcp-protocol-version") == "2025-11-25"
    assert script.bodies()[3]["params"] == {"name": "count_mail", "arguments": {"folder": "INBOX"}}
    assert all(request.full_url == "http://127.0.0.1:8765/mcp" for request in script.requests)
    assert len(said) == 2 and all(line.lstrip().startswith("ok:") for line in said)
    assert TOKEN not in " ".join(said) and "3" not in said[1]


def test_client_accepts_a_stateless_json_reply_and_structured_content():
    init = Reply(body=json.dumps(initialized(1)).encode())
    result = tool_ok(2)
    result["result"]["structuredContent"] = {"folder": "INBOX", "count": 3}
    script = Script(refused(), init, Reply(202), Reply(body=json.dumps(result).encode()))
    assert len(run_check(script)) == 2
    assert script.requests[3].get_header("Mcp-session-id") is None


@pytest.mark.parametrize(
    ("first", "text"),
    [
        (Reply(200, b"{}"), "without the bearer token"),
        (refused(404), "answered 404 without the bearer token"),
        (ConnectionRefusedError(), "no usable answer"),
        (TimeoutError(), "no usable answer"),
    ],
)
def test_client_refuses_a_port_that_is_not_this_server_before_sending_the_token(first, text):
    script = Script(first)
    with pytest.raises(selfcheck.SelfCheckError, match=text) as caught:
        run_check(script)
    assert len(script.requests) == 1
    assert script.requests[0].get_header("Authorization") is None
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    ("second", "text"),
    [
        (refused(401), "refused the stored token"),
        (refused(500), "did not initialize as proton-mail-mcp"),
        (Reply(body=b"{}"), "did not initialize as proton-mail-mcp"),
        (Reply(body=json.dumps(initialized(1, "other")).encode()), "did not initialize"),
        (Reply(body=b"not json"), "no usable answer"),
        (Reply(body=b"x" * (selfcheck._MAX_REPLY_BYTES + 1)), "no usable answer"),
    ],
)
def test_client_stops_when_the_token_or_the_server_is_wrong(second, text):
    with pytest.raises(selfcheck.SelfCheckError, match=text) as caught:
        run_check(Script(refused(), second))
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "bad",
    [
        {"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": []}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "no"}},
        tool_ok(2, partial=True, timed_out=True),
        {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "plain"}]}},
    ],
    ids=["tool-error", "rpc-error", "timed-out", "unreadable"],
)
def test_client_fails_when_bridge_cannot_answer_after_waiting(bad):
    pauses = []
    replies = []
    for attempt in range(selfcheck._BRIDGE_ATTEMPTS):
        replies.append(sse({**bad, "id": attempt + 2}))
    script = Script(refused(), sse(initialized(1)), Reply(202), *replies)
    with pytest.raises(selfcheck.SelfCheckError, match="cannot read INBOX through Bridge"):
        selfcheck.check_service(8765, TOKEN, lambda _t: None, open_url=script, sleep=pauses.append)
    assert pauses == [selfcheck._BRIDGE_PAUSE] * (selfcheck._BRIDGE_ATTEMPTS - 1)


def test_client_survives_a_reply_that_is_not_an_object():
    with pytest.raises(selfcheck.SelfCheckError, match="did not initialize"):
        run_check(Script(refused(), Reply(body=b"[1, 2]")))


def test_client_drops_a_protocol_version_that_is_not_text():
    reply = initialized(1)
    reply["result"]["protocolVersion"] = 7
    script = Script(refused(), Reply(body=json.dumps(reply).encode()), Reply(202), sse(tool_ok(2)))
    assert len(run_check(script)) == 2
    assert script.requests[3].get_header("Mcp-protocol-version") is None


def test_client_waits_for_a_bridge_that_is_still_starting():
    bad = {"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": []}}
    script = Script(refused(), sse(initialized(1)), Reply(202), sse(bad), sse(tool_ok(3)))
    assert len(run_check(script)) == 2


def test_client_ignores_other_stream_messages_and_stops_at_its_reply():
    note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}
    other = {"jsonrpc": "2.0", "id": 99, "result": {}}
    data = b"".join(
        b"data: " + json.dumps(item).encode() + b"\n\n" for item in (note, other, initialized(1))
    )
    # What follows the reply must never be read: a kept-open stream would hang here.
    reply = Reply(body=data + b"data: {broken", kind="text/event-stream")
    script = Script(refused(), reply, Reply(202), sse(tool_ok(2)))
    assert len(run_check(script)) == 2


def test_client_never_follows_a_redirect_with_the_token():
    handler = selfcheck._NoRedirects()
    assert handler.redirect_request(None, None, 302, "x", {}, "http://evil.example/") is None


def test_client_bypasses_proxies(monkeypatch):
    # http_proxy in the shell must not see a request that carries the token.
    built = []

    def build(*handlers):
        built.extend(handlers)
        return SimpleNamespace(open=lambda request, timeout: request)

    monkeypatch.setattr(selfcheck.urllib.request, "build_opener", build)
    selfcheck._default_open(object(), 1.0)
    proxy = next(h for h in built if isinstance(h, selfcheck.urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    assert any(isinstance(h, selfcheck._NoRedirects) for h in built)


# Package data --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        templates.BRIDGE_UNIT,
        templates.MCP_UNIT,
        f"{templates.BRIDGE_LABEL}.plist",
        f"{templates.MCP_LABEL}.plist",
    ],
)
def test_service_templates_are_package_data_inside_the_package(name):
    path = ROOT / "src" / "proton_mail_mcp" / "installer" / "service_templates" / name
    assert path.is_file()
    assert templates._template(name) == path.read_text(encoding="utf-8")


def test_the_mcp_plist_keeps_its_exact_bytes():
    text = templates.launchd_mcp_plist("/a&b/svc", "/a&b/env", "/a&b", "/a&b/Logs")
    assert text == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f"<!-- {MARKER} -->\n"
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>KeepAlive</key>\n\t<true/>\n"
        "\t<key>Label</key>\n\t<string>com.proton-mail-mcp</string>\n"
        "\t<key>ProcessType</key>\n\t<string>Background</string>\n"
        "\t<key>ProgramArguments</key>\n\t<array>\n"
        "\t\t<string>/a&amp;b/svc</string>\n"
        "\t\t<string>--env-file</string>\n"
        "\t\t<string>/a&amp;b/env</string>\n\t</array>\n"
        "\t<key>RunAtLoad</key>\n\t<true/>\n"
        "\t<key>StandardErrorPath</key>\n\t<string>/a&amp;b/Logs/stderr.log</string>\n"
        "\t<key>StandardOutPath</key>\n\t<string>/a&amp;b/Logs/stdout.log</string>\n"
        "\t<key>ThrottleInterval</key>\n\t<integer>10</integer>\n"
        "\t<key>Umask</key>\n\t<integer>63</integer>\n"
        "\t<key>WorkingDirectory</key>\n\t<string>/a&amp;b</string>\n"
        "</dict>\n</plist>\n"
    )


def test_the_bridge_plist_keeps_its_exact_bytes():
    text = templates.launchd_bridge_plist("/Apps/$HOME/exe", "/Logs")
    assert text == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f"<!-- {MARKER} -->\n"
        '<plist version="1.0">\n'
        "<dict>\n"
        "\t<key>KeepAlive</key>\n\t<dict>\n"
        "\t\t<key>SuccessfulExit</key>\n\t\t<false/>\n\t</dict>\n"
        "\t<key>Label</key>\n\t<string>com.proton-mail-mcp.bridge</string>\n"
        "\t<key>ProgramArguments</key>\n\t<array>\n"
        "\t\t<string>/Apps/$HOME/exe</string>\n"
        "\t\t<string>--noninteractive</string>\n\t</array>\n"
        "\t<key>RunAtLoad</key>\n\t<true/>\n"
        "\t<key>StandardErrorPath</key>\n\t<string>/Logs/bridge-stderr.log</string>\n"
        "\t<key>StandardOutPath</key>\n\t<string>/Logs/bridge-stdout.log</string>\n"
        "\t<key>ThrottleInterval</key>\n\t<integer>30</integer>\n"
        "\t<key>Umask</key>\n\t<integer>63</integer>\n"
        "</dict>\n</plist>\n"
    )


def test_a_template_value_is_never_read_as_a_template():
    unit = templates.systemd_mcp_unit("/opt/$SERVICE_EXE/svc", "/opt/${PORT}", 1, False)
    assert "$$SERVICE_EXE" in unit and "$${PORT}" in unit
    plist = templates.launchd_mcp_plist("/${LOG_DIR}", "/${WORKDIR}", "/w", "/l")
    assert "/${LOG_DIR}" in plist and "/${WORKDIR}" in plist


# `setup --dry-run` on Linux, through the real host probes ------------------------------


STAND_IN = """#!/bin/sh
# Records every call; only systemd's read-only queries and "no entry" are answered.
printf '%s\\n' "$(basename "$0") $*" >> "$PMM_TEST_CALLS"
case "$(basename "$0") $1 $2" in
  "systemctl --user show-environment") echo HOME=/nowhere ;;
  "systemctl --user show") echo ;;
  *) exit 1 ;;
esac
"""


@pytest.mark.skipif(
    sys.platform != "linux" or not detect_host().supported,
    reason="needs a supported Linux host",
)
def test_setup_dry_run_on_linux_through_the_real_probes_changes_nothing(tmp_path):
    stand_ins = tmp_path / "bin"
    stand_ins.mkdir()
    for name in ("systemctl", "secret-tool"):
        path = stand_ins / name
        path.write_text(STAND_IN)
        path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    calls = tmp_path / "calls"
    env = {
        "PATH": f"{stand_ins}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PMM_TEST_CALLS": str(calls),
    }
    assert shutil.which("systemctl", path=env["PATH"]) == str(stand_ins / "systemctl")
    code = "import sys; from proton_mail_mcp.cli import main; raise SystemExit(main(sys.argv[1:]))"
    argv = [sys.executable, "-P", "-c", code, "setup", "--dry-run", "--skip-bridge"]
    result = REAL_RUN(
        [*argv, "--skip-bridge-service"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if REAL_EUID == 0:
        assert result.returncode == 2 and "normal user" in result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry run: nothing was changed." in result.stdout
    assert f"this plan shows {cli.PLACEHOLDER_ACCOUNT}" in result.stdout
    assert str(home) in result.stdout
    assert "Check the MCP server answers" in result.stdout
    assert list(home.iterdir()) == []
    recorded = calls.read_text().splitlines()
    assert recorded, "the plan reads systemd"
    # Read-only queries only: no start, stop, enable, reload or credential store.
    for call in recorded:
        words = call.split()
        assert words[:2] == ["systemctl", "--user"] and words[2] in {"show-environment", "show"}


@pytest.mark.skipif(shutil.which("bash") is None or os.name != "posix", reason="needs bash")
def test_install_sh_runs_setup_from_a_checkout_with_the_standard_library_alone():
    result = REAL_RUN(
        ["bash", str(ROOT / "scripts" / "install.sh"), "setup", "--dry-run", "--simulate", "arch"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nowhere", "PMM_PYTHON": sys.executable},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("setup plan for Arch Linux")
    assert f"Install proton-mail-mcp from {ROOT}" in result.stdout


def test_closed_standard_streams_count_as_no_terminal(monkeypatch):
    # Python sets these to None when a process starts with them closed, as agents may.
    monkeypatch.setattr(sys, "stdin", None)
    assert execute.on_terminal() is False
    monkeypatch.setattr(execute.os, "open", lambda *_args: (_ for _ in ()).throw(OSError()))
    assert execute.terminal_available() is False


def test_setup_with_stdin_closed_refuses_instead_of_crashing(host, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.main(["setup"]) == 2
    assert "Pass --bridge-user" in capsys.readouterr().err
    assert cli.main(["setup", "--bridge-user", USER]) == 2
    assert "confirm with --yes" in capsys.readouterr().err


def test_terminal_available_falls_back_to_stdin(monkeypatch):
    def no_tty(*_args):
        raise OSError

    monkeypatch.setattr(execute.os, "open", no_tty)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert execute.terminal_available() is False
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert execute.terminal_available() is True


def test_fake_probe_answers_like_the_protocol():
    assert FakeProbe().can_make_venv("python") is True
