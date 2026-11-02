"""The `proton-mail-mcp` command: serve by default, installer commands, no server import."""

import os
import plistlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from proton_mail_mcp import cli
from proton_mail_mcp.installer import cli as installer_cli
from proton_mail_mcp.installer import templates
from proton_mail_mcp.installer.detect import detect

ROOT = Path(__file__).resolve().parents[1]
# conftest forbids subprocess.run during tests; these tests run local Python only.
REAL_RUN = subprocess.run
SOURCE = str(ROOT / "src")
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]


@pytest.fixture
def served(monkeypatch):
    """A stand-in server module, so these tests also run where fcntl is missing."""
    calls = []
    server = SimpleNamespace(main=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setitem(sys.modules, "proton_mail_mcp.server", server)
    return calls


def python(code: str, *argv: str) -> subprocess.CompletedProcess:
    return REAL_RUN(
        [sys.executable, "-P", "-c", code, *argv],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": SOURCE, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_no_arguments_and_serve_start_the_server_as_the_old_entry_point_did(served, argv):
    assert cli.main(argv) == 0
    # server.main() with nothing passed reads os.environ, as the old console script did.
    assert served == [((), {})]


def test_the_command_line_defaults_to_sys_argv(served, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["proton-mail-mcp"])
    assert cli.main() == 0
    assert served == [((), {})]


def test_serve_takes_no_options_and_never_starts_on_a_typo(served, capsys):
    assert cli.main(["serve", "--port", "1"]) == 2
    assert served == []
    assert "unknown arguments: serve --port 1" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["setup", "uninstall", "show-token", "detect"])
def test_installer_commands_go_to_the_installer_and_never_the_server(served, monkeypatch, command):
    calls = []
    monkeypatch.setattr(
        installer_cli, "main", lambda argv, prog: calls.append((argv, prog)) or 7, raising=True
    )
    assert cli.main([command, "--dry-run"]) == 7
    assert calls == [([command, "--dry-run"], "proton-mail-mcp")]
    assert served == []


def test_install_is_not_a_command_here_so_it_cannot_start_the_server(served, capsys):
    assert cli.main(["install"]) == 2
    assert served == []
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--dry-run", "--port=1", "bogus"])
def test_unknown_arguments_print_usage_and_exit_2(served, capsys, flag):
    assert cli.main([flag]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: proton-mail-mcp ")
    assert served == []


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_names_every_command(capsys, flag):
    assert cli.main([flag]) == 0
    out = capsys.readouterr().out
    for command in ("serve", "setup", "uninstall", "show-token", "detect"):
        assert re.search(rf"^  {command} ", out, re.MULTILINE), command


def test_version_comes_from_the_installed_distribution(monkeypatch, capsys):
    monkeypatch.setattr(cli.metadata, "version", lambda name: f"9.9.9 of {name}")
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == "proton-mail-mcp 9.9.9 of mcp-proton-mail\n"


def test_version_without_a_distribution_says_so(monkeypatch, capsys):
    def missing(_name):
        raise cli.metadata.PackageNotFoundError

    monkeypatch.setattr(cli.metadata, "version", missing)
    assert cli.main(["--version"]) == 0
    assert "version unknown" in capsys.readouterr().out


def test_a_startup_refusal_keeps_its_status_and_points_at_setup(monkeypatch, capsys):
    def refuse():
        raise SystemExit(2)

    monkeypatch.setitem(sys.modules, "proton_mail_mcp.server", SimpleNamespace(main=refuse))
    with pytest.raises(SystemExit) as caught:
        cli.main([])
    assert caught.value.code == 2
    assert "`proton-mail-mcp setup`" in capsys.readouterr().err


@pytest.mark.parametrize("code", [3, 0, None])
def test_any_other_server_exit_passes_through_untouched(monkeypatch, capsys, code):
    def leave():
        raise SystemExit(code)

    monkeypatch.setitem(sys.modules, "proton_mail_mcp.server", SimpleNamespace(main=leave))
    with pytest.raises(SystemExit) as caught:
        cli.main(["serve"])
    assert caught.value.code == code
    assert capsys.readouterr().err == ""


def test_serve_on_windows_starts_the_server(served, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli.main([]) == 0
    assert served == [((), {})]


NO_FCNTL = """
import platform, sys
sys.modules["fcntl"] = None
platform.system = lambda: {system!r}
platform.machine = lambda: "AMD64"
from proton_mail_mcp.cli import main
code = main(sys.argv[1:])
assert "proton_mail_mcp.server" not in sys.modules
raise SystemExit(code)
"""


@pytest.mark.parametrize("system", ["CYGWIN_NT-10.0-26100", "MSYS_NT-10.0-26100"])
@pytest.mark.parametrize(
    "argv", [["setup"], ["setup", "--dry-run"], ["uninstall", "--yes"], ["show-token"]]
)
def test_a_windows_layer_is_refused_before_it_needs_the_server(system, argv):
    result = python(NO_FCNTL.format(system=system), *argv)
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr.startswith(f"proton-mail-mcp: Unsupported platform: {system}")
    assert "Python for Windows" in result.stderr and "WSL2" in result.stderr
    assert "Traceback" not in result.stderr


def test_detect_runs_on_windows_without_the_server():
    result = python(NO_FCNTL.format(system="Windows"), "detect", "--json")
    assert result.returncode == 0, result.stderr
    assert '"supported": true' in result.stdout and "refusal" not in result.stdout


@pytest.mark.parametrize("system", ["CYGWIN_NT-10.0", "MSYS_NT-10.0", "MINGW64_NT-10.0"])
def test_windows_compatibility_layers_do_not_count_as_windows(system):
    # install.sh refuses these in shell before Python runs; a wheel has no such shell step.
    assert detect(system, "x86_64").family == "unsupported"


def test_the_console_script_is_the_dispatcher_and_the_service_entry_is_unchanged():
    scripts = PROJECT["scripts"]
    assert scripts["proton-mail-mcp"] == "proton_mail_mcp.cli:main"
    # `uvx mcp-proton-mail` runs the command named like the distribution: the same one.
    assert scripts["mcp-proton-mail"] == scripts["proton-mail-mcp"]
    # Installed units and LaunchAgents run this launcher, not the dispatcher.
    assert scripts["proton-mail-mcp-service"] == "proton_mail_mcp.service_entry:main"
    assert "proton-mail-mcp-service --env-file" in templates.systemd_mcp_unit(
        "proton-mail-mcp-service", "settings", 8765, True
    )


def test_the_example_launchagent_still_starts_the_server_through_the_bare_command():
    with (ROOT / "examples" / "com.proton-mail-mcp.plist").open("rb") as handle:
        program = plistlib.load(handle)["ProgramArguments"]
    assert len(program) == 1 and program[0].endswith("/bin/proton-mail-mcp")


def test_the_license_file_is_the_whole_apache_license_with_its_owner():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith(
        "Apache License\n                           Version 2.0, January 2004"
    )
    assert "END OF TERMS AND CONDITIONS" in text
    assert "   Copyright 2026 Dmitri Manajev\n" in text
    assert "[yyyy]" not in text and "MIT License" not in text


def test_the_version_has_one_source_that_the_wheel_reads():
    # A version written in pyproject.toml too could drift from the one the server reports.
    assert "version" not in PROJECT and PROJECT["dynamic"] == ["version"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/proton_mail_mcp/__init__.py"
    text = (ROOT / "src" / "proton_mail_mcp" / "__init__.py").read_text(encoding="utf-8")
    [version] = re.findall(r'^__version__ = "([^"]+)"$', text, flags=re.M)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    from proton_mail_mcp import __version__

    assert __version__ == version


def test_package_metadata_is_complete():
    # PyPI refused proton-mail-mcp as too close to another project's name; the command
    # keeps it. --version and the installer look the distribution up by this name.
    assert PROJECT["name"] == "mcp-proton-mail" == cli._DIST == installer_cli.PACKAGE
    assert PROJECT["requires-python"] == ">=3.12"
    assert PROJECT["readme"] == "README.md"
    assert PROJECT["license"] == "Apache-2.0"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert PROJECT["authors"] == [{"name": "Dmitri Manajev", "email": "dmitri@manajev.com"}]
    assert "not affiliated" in PROJECT["description"].lower()
    assert set(PROJECT["urls"]) >= {"Homepage", "Repository", "Documentation", "Issues"}
    assert all(
        url.startswith("https://github.com/legalaspro/proton-mail-mcp")
        for url in PROJECT["urls"].values()
    )
    classifiers = PROJECT["classifiers"]
    assert "Operating System :: POSIX :: Linux" in classifiers
    assert "Operating System :: MacOS" in classifiers
    # Kept beside the expression on purpose. PEP 639 deprecates such classifiers, but
    # hatchling builds them, and PyPI's upload checks have no rule against them (read in
    # its source on 31 Oct 2026; nothing has been uploaded).
    assert [name for name in classifiers if name.startswith("License ::")] == [
        "License :: OSI Approved :: Apache Software License"
    ]
    # No classifier may claim Windows before it has run on a desktop: see docs/windows.md.
    assert not [name for name in classifiers if "Windows" in name]


def test_the_wheel_lists_files_that_exist():
    wheel = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/proton_mail_mcp"]
    for source, target in wheel["force-include"].items():
        assert (ROOT / source).is_file(), source
        assert target.startswith("proton_mail_mcp/examples/"), target


def test_nothing_in_this_repository_uploads_a_package():
    # Publishing needs its own GO, a v* tag in the release repo, and goes only through
    # publish.yml (test_ci_workflows.py holds it to that): nothing else may do it by accident.
    files = [
        ROOT / "Makefile",
        *(ROOT / "scripts").glob("*"),
        *(ROOT / ".github" / "workflows").glob("*"),
    ]
    words = ("twine", "uv publish", "hatch publish", "upload.pypi", "test.pypi")
    for path in files:
        if path.is_file():
            text = path.read_text(encoding="utf-8").lower()
            for word in words + (() if path.name == "publish.yml" else ("pypi-publish",)):
                assert word not in text, f"{path}: {word}"
