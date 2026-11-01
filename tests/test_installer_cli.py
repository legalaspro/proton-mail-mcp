"""Installer command line and its shell and PowerShell entry points."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from proton_mail_mcp import credential_store
from proton_mail_mcp.installer import cli
from proton_mail_mcp.installer.detect import detect, simulated
from proton_mail_mcp.installer.plan import MANAGER_ENVIRONMENT, SYSTEMD_BOOTED
from proton_mail_mcp.installer.probe import simulated_probe
from proton_mail_mcp.installer.steps import MARKER

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"
# conftest forbids subprocess.run during tests; these tests run local scripts only.
REAL_RUN = subprocess.run
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None or os.name != "posix", reason="needs POSIX bash")


@pytest.fixture
def host(monkeypatch):
    """Make the real-host code path plan for a simulated Arch machine."""
    platform = simulated("arch")
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", simulated_probe)
    return platform


def test_default_source_is_this_checkout():
    assert cli.default_source() == str(ROOT)
    assert not cli.is_source(ROOT / "scripts")


@pytest.mark.parametrize("family", ["debian", "fedora", "arch", "macos"])
def test_simulated_dry_runs_succeed_for_every_supported_platform(family, capsys):
    code = cli.main(
        ["install", "--dry-run", "--simulate", family, "--bridge-user", "you@example.com"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run: nothing was changed." in out
    assert "http://127.0.0.1:8765/mcp" in out
    code = cli.main(["uninstall", "--dry-run", "--simulate", family])
    assert code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["install", "--bridge-user", "a@b.c", "--yes"],
        ["uninstall", "--yes"],
        ["show-token", "--bridge-user", "a@b.c"],
    ],
)
def test_a_windows_layer_is_refused_before_any_plan_or_change(argv, monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_host", lambda: detect("MSYS_NT-10.0", "x86_64"))
    monkeypatch.setattr(cli, "Executor", lambda **kwargs: pytest.fail("executor built"))
    monkeypatch.setattr(cli, "HostProbe", lambda platform: pytest.fail("host probed"))
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("install: Unsupported platform: MSYS_NT-10.0")
    assert "Python for Windows" in captured.err
    assert captured.out == ""


def test_flags_default_to_install(capsys):
    assert cli.main(["--dry-run", "--simulate", "arch", "--bridge-user", "a@b.c"]) == 0
    assert capsys.readouterr().out.startswith("install plan for Arch Linux")


def test_simulation_never_applies(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["install", "--simulate", "arch", "--bridge-user", "a@b.c"])
    assert caught.value.code == 2
    assert "--dry-run" in capsys.readouterr().err


def test_plan_errors_exit_2_before_any_change(capsys):
    assert cli.main(["install", "--dry-run", "--simulate", "arch"]) == 2
    assert "--bridge-user" in capsys.readouterr().err


def test_source_must_be_a_checkout(capsys, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["install", "--dry-run", "--simulate", "arch", "--source", str(tmp_path)])


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX only")
def test_root_is_refused(host, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert cli.main(["install", "--dry-run", "--bridge-user", "a@b.c"]) == 2
    assert "normal user" in capsys.readouterr().err


def test_applying_needs_a_terminal_or_yes(host, monkeypatch, capsys):
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "Executor", guarded_executor(cli.Executor))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["install", "--bridge-user", "a@b.c"]) == 2
    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "[dry run]" in captured.out


def guarded_executor(real):
    def factory(*, dry_run, **kwargs):
        assert dry_run, "the confirmation gate let a real run through"
        return real(dry_run=dry_run, **kwargs)

    return factory


def test_detect_reports_json(capsys):
    assert cli.main(["detect", "--json", "--simulate", "macos"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["family"] == "macos" and report["supported"] is True
    assert report["credential_store"] == "keychain"
    assert report["service_manager"] == "launchd LaunchAgents"
    assert cli.main(["detect", "--json", "--simulate", "windows"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["family"] == "windows" and report["supported"] is True
    assert report["credential_store"] == "wincred"
    assert report["service_manager"] == "Task Scheduler logon tasks"
    assert "refusal" not in report


@pytest.mark.skipif(sys.platform == "win32", reason="names POSIX paths")
def test_venv_option_reaches_install_and_uninstall_plans(monkeypatch, capsys):
    base = ["--dry-run", "--simulate", "arch", "--bridge-user", "a@b.c", "--venv", "/opt/pmm"]
    assert cli.main(["install", *base]) == 0
    out = capsys.readouterr().out
    assert "$ /opt/pmm/bin/python -m pip install" in out
    assert "/opt/pmm/.proton-mail-mcp-installer" in out

    def installed(platform):
        probe = simulated_probe(platform)
        probe.files.update({"/opt/pmm": "", "/opt/pmm/.proton-mail-mcp-installer": "marker"})
        unit = "/home/you/.config/systemd/user/proton-mail-mcp.service"
        probe.files[unit] = "# Managed by proton-mail-mcp installer\n"
        return probe

    # Uninstall keeps the environment and prints how to remove it (round 7).
    monkeypatch.setattr(cli, "simulated_probe", installed)
    assert cli.main(["uninstall", *base[:-2], "--venv", "/opt/pmm"]) == 0
    out = capsys.readouterr().out
    assert "The virtual environment /opt/pmm (6 bytes) stays." in out
    assert "remove it with: `rm -rf -- /opt/pmm`" in out
    assert "/opt/pmm: remove" not in out


class Terminal:
    def __init__(self):
        self.text = ""

    def isatty(self):
        return True

    def write(self, text):
        self.text += text

    def flush(self):
        pass


def test_show_token_prints_only_to_a_terminal(host, monkeypatch, capsys):
    calls = []

    def lookup(store, service, account, locations=None):
        calls.append((store, service, account, locations))
        return "stored-token-value"

    monkeypatch.setattr(credential_store, "lookup", lookup)
    assert cli.main(["show-token", "--bridge-user", "a@b.c"]) == 2
    assert "only to a terminal" in capsys.readouterr().err
    assert calls == []
    terminal = Terminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    assert cli.main(["show-token", "--bridge-user", "a@b.c"]) == 0
    assert terminal.text == "stored-token-value\n"
    assert calls == [("libsecret", credential_store.TOKEN_SERVICE, "a@b.c", {})]


@pytest.mark.parametrize("family", ["fedora", "arch"])
def test_a_container_dry_run_goes_through_preflight_and_plans_no_services(
    family, monkeypatch, capsys
):
    # The CI containers start no systemd, and preflight refused their dry runs.
    # Round 6 (c): preflight sees the container and the plan goes on without services.
    platform = simulated(family)
    probe = simulated_probe(platform)
    del probe.files[SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    probe.files["/.dockerenv"] = ""
    # The CI containers run pytest as root, and the CLI refuses root.
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", lambda _platform: probe)
    assert cli.main(["install", "--dry-run", "--bridge-user", "a@b.c"]) == 0
    out = capsys.readouterr().out
    assert "Services: this host is a container (docker) that does not run systemd" in out
    assert "systemctl --user" not in out and "Dry run: nothing was changed." in out
    assert cli.main(["uninstall", "--dry-run"]) == 0
    assert "so only files are removed" in capsys.readouterr().out


def test_r10_ci_linux_dry_runs_skip_the_credential_check_they_cannot_make(monkeypatch, capsys):
    # secret-tool cannot answer on the CI runners, so the credential check
    # stopped the native dry runs (exit 2) before their service-preflight greps.
    workflows = ROOT / ".github" / "workflows"
    workflow = "".join(
        (workflows / name).read_text(encoding="utf-8") for name in ("check.yml", "installer.yml")
    )
    assert workflow.count("install --dry-run --skip-credentials --bridge-user") == 3
    platform = simulated("fedora")
    probe = simulated_probe(platform)
    del probe.files[SYSTEMD_BOOTED]
    del probe.outputs[MANAGER_ENVIRONMENT]
    probe.has_secret = lambda *args, **kwargs: None  # the store cannot be checked
    # A normal user, so the first exit 2 is the credential check's, not root's.
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", lambda _platform: probe)
    assert cli.main(["install", "--dry-run", "--bridge-user", "a@b.c"]) == 2
    argv = ["install", "--dry-run", "--skip-credentials", "--bridge-user", "a@b.c"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "so the installer sets up no service, as with --skip-services" in out


def test_show_token_reads_the_pass_store_the_service_reads(host, monkeypatch):
    # A store chosen through the installing shell's PASSWORD_STORE_DIR.
    probe = simulated_probe(host)
    paths = cli.paths_for(host, probe.env, probe.home)
    probe.files[paths.env_file] = (
        f"# {MARKER}\nPROTON_BRIDGE_USER=a@b.c\nPROTON_MCP_CREDENTIAL_STORE=pass\n"
        "PROTON_MCP_PASSWORD_STORE_DIR=/srv/pass\nPROTON_MCP_GNUPGHOME=/srv/gnupg\n"
    )
    probe.env["PASSWORD_STORE_DIR"] = "/home/you/another-store"
    monkeypatch.setattr(cli, "HostProbe", lambda platform: probe)
    calls = []
    monkeypatch.setattr(
        credential_store, "lookup", lambda *args: calls.append(args) or "stored-token-value"
    )
    monkeypatch.setattr(sys, "stdout", Terminal())
    assert cli.main(["show-token"]) == 0
    located = {"PASSWORD_STORE_DIR": "/srv/pass", "GNUPGHOME": "/srv/gnupg"}
    assert calls == [("pass", credential_store.TOKEN_SERVICE, "a@b.c", located)]


def test_a_relative_bridge_bin_is_resolved_against_the_current_directory(tmp_path, monkeypatch):
    # `--bridge-bin ./bin/bridge` became ExecStart=./bin/bridge, which systemd rejects.
    monkeypatch.chdir(tmp_path)
    parser = cli.build_parser()
    options = cli._options(parser.parse_args(["install", "--bridge-bin", "bin/bridge"]))
    assert options.bridge_bin == os.path.join(os.getcwd(), "bin", "bridge")
    assert os.path.isabs(options.bridge_bin)
    # A simulated plan names another system's paths; the plan refuses relative ones.
    simulate = ["install", "--simulate", "debian", "--bridge-bin", "bin/bridge"]
    assert cli._options(parser.parse_args(simulate)).bridge_bin == "bin/bridge"


def test_a_simulated_relative_bridge_bin_is_refused(capsys):
    argv = ["install", "--dry-run", "--simulate", "arch", "--bridge-user", "a@b.c"]
    code = cli.main([*argv, "--skip-bridge", "--bridge-bin", "bin/bridge"])
    assert code == 2
    assert "--bridge-bin must be an absolute path" in capsys.readouterr().err


def run_script(args, **env):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
    return REAL_RUN(
        [BASH, str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env={**base, **env},
        timeout=60,
        check=False,
    )


@needs_bash
def test_install_sh_is_valid_bash():
    result = REAL_RUN([BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    shutil.which("shellcheck") is None or os.name != "posix", reason="needs POSIX shellcheck"
)
def test_install_sh_passes_shellcheck():
    result = REAL_RUN(
        ["shellcheck", "--shell=bash", str(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_bash
def test_install_sh_runs_the_module_from_this_checkout(tmp_path):
    result = run_script(
        ["detect", "--json", "--simulate", "fedora"],
        PMM_PYTHON=sys.executable,
        PYTHONPATH=str(tmp_path / "ignored"),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["family"] == "fedora"
    dry = run_script(
        ["install", "--dry-run", "--simulate", "debian", "--bridge-user", "a@b.c"],
        PMM_PYTHON=sys.executable,
    )
    assert dry.returncode == 0, dry.stderr
    assert "debsig-verify" in dry.stdout and "Dry run: nothing was changed." in dry.stdout


@needs_bash
@pytest.mark.parametrize(
    ("ostype", "os_id", "hint"),
    [
        ("linux-gnu", "fedora", "sudo dnf install python3"),
        ("linux-gnu", "ubuntu", "sudo apt-get install python3 python3-venv"),
        ("linux-gnu", "arch", "sudo pacman -S python"),
        # macOS wins over any os-release file.
        ("darwin23", "ubuntu", "brew install python@3.12"),
    ],
    ids=["fedora", "ubuntu", "arch", "macos"],
)
def test_install_sh_explains_a_missing_python(tmp_path, ostype, os_id, hint):
    # bash keeps an OSTYPE it inherits, so each case sees the same system on every host
    # (the Linux cases printed the Homebrew hint on the macOS runner).
    release = tmp_path / "os-release"
    release.write_text(f'ID={os_id}\nVERSION_ID="1"\n')
    empty = tmp_path / "bin"
    empty.mkdir()
    result = run_script(["detect"], PATH=str(empty), PMM_OS_RELEASE=str(release), OSTYPE=ostype)
    assert result.returncode == 3
    assert "Python 3.12 or newer is required" in result.stderr
    assert hint in result.stderr
    managers = ("sudo dnf", "sudo apt-get", "sudo pacman", "brew install")
    assert [name for name in managers if name in result.stderr] == [
        name for name in managers if hint.startswith(name)
    ]


def test_install_ps1_only_forwards_flags_the_module_accepts():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    forwarded = set(re.findall(r"'(--[a-z][a-z-]+)'", text))
    assert forwarded
    accepted = set()
    parser = cli.build_parser()
    for action in parser._subparsers._group_actions:
        for subparser in action.choices.values():
            for option in subparser._actions:
                accepted.update(option.option_strings)
    assert forwarded <= accepted, forwarded - accepted
    assert "-P -s -m proton_mail_mcp.installer" in text
    assert "ValidateSet('install', 'uninstall', 'detect', 'show-token')" in text


def test_install_ps1_only_finds_python_and_runs_the_module():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "$refusal" not in text and "not yet supported" not in text
    assert text.index("$found = Find-Python") < text.index("-m proton_mail_mcp.installer")


def test_windows_job_test_modules_import_without_fcntl():
    """The Windows CI job collects these files; none may import the server at load."""
    modules = [
        "test_installer_detect",
        "test_installer_plan",
        "test_installer_execute",
        "test_installer_cli",
        "test_installer_upgrade",
        "test_installer_setup",
        "test_cli_dispatch",
        "test_credential_store",
        "test_service_entry",
    ]
    workflow = (ROOT / ".github" / "workflows" / "installer.yml").read_text(encoding="utf-8")
    assert all(f"tests/{name}.py" in workflow for name in modules)
    code = (
        "import importlib, sys; sys.modules['fcntl'] = None; "
        f"sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'tests')!r}]; "
        f"[importlib.import_module(name) for name in {modules!r}]; "
        "assert 'proton_mail_mcp.server' not in sys.modules"
    )
    result = REAL_RUN(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 not installed")
def test_install_ps1_parses_and_plans_every_platform():
    parse = (
        "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    result = REAL_RUN(
        ["pwsh", "-NoProfile", "-Command", parse], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

    def ps1(*args):
        return REAL_RUN(
            ["pwsh", "-NoProfile", "-File", str(INSTALL_PS1), *args, "-Python", sys.executable],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    planned = ps1("-DryRun", "-Simulate", "windows", "-BridgeUser", "you@example.com")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "schtasks" in planned.stdout
    planned = ps1("-DryRun", "-Simulate", "debian", "-BridgeUser", "you@example.com")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert "debsig-verify" in planned.stdout
