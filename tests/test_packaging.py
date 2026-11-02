"""The registry entry and the Claude Desktop bundle agree with the package they describe."""

import importlib.util
import json
import re
import tomllib
import zipfile
from pathlib import Path

from proton_mail_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))


def builder():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build_mcpb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_registry_entry_runs_connect_from_this_version_on_pypi():
    assert SERVER["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert SERVER["name"] == "io.github.legalaspro/proton-mail-mcp"
    assert 1 <= len(SERVER["description"]) <= 100
    assert SERVER["version"] == __version__
    (package,) = SERVER["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "mcp-proton-mail"
    assert package["version"] == __version__
    assert package["runtimeHint"] == "uvx"
    assert package["transport"] == {"type": "stdio"}
    assert package["packageArguments"] == [{"type": "positional", "value": "connect"}]


def test_the_readme_names_the_registry_entry_for_pypi_to_prove_ownership():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    named = re.findall(r"mcp-name: ([a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+)(?=\s|-->|<|$)", readme)
    assert named == [SERVER["name"]]


def test_the_bundle_manifest_runs_connect_with_uv_from_this_version():
    assert MANIFEST["manifest_version"] == "0.4"
    assert MANIFEST["version"] == __version__
    server = MANIFEST["server"]
    assert server["type"] == "uv"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == [
        "run",
        "--directory",
        "${__dirname}",
        server["entry_point"],
    ]
    assert set(MANIFEST["compatibility"]["platforms"]) == {"darwin", "linux", "win32"}
    assert MANIFEST["compatibility"]["runtimes"]["python"] == ">=3.12"
    with (ROOT / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["requires-python"] == ">=3.12"
    entry = (ROOT / "mcpb" / "main.py").read_text(encoding="utf-8")
    assert 'main(["connect", *sys.argv[1:]])' in entry


def test_the_bundle_holds_what_uv_needs_to_install_the_project(tmp_path):
    target = builder().build(tmp_path)
    assert target.name == f"proton-mail-mcp-{__version__}.mcpb"
    with zipfile.ZipFile(target) as bundle:
        names = set(bundle.namelist())
        assert json.loads(bundle.read("manifest.json")) == MANIFEST
        assert (
            bundle.read(MANIFEST["server"]["entry_point"])
            == (ROOT / "mcpb" / "main.py").read_bytes()
        )
    with (ROOT / "pyproject.toml").open("rb") as stream:
        wheel = tomllib.load(stream)["tool"]["hatch"]["build"]["targets"]["wheel"]
    expected = {
        "manifest.json",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/proton_mail_mcp/__init__.py",
        "src/proton_mail_mcp/cli.py",
        "src/proton_mail_mcp/connect.py",
        *wheel["force-include"],
    }
    assert expected <= names, expected - names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    # The host installs the dependencies; a bundled environment is not allowed for uv.
    assert not any(name.startswith(("server/lib/", "server/venv/", ".venv/")) for name in names)


def test_the_same_checkout_builds_the_same_bundle(tmp_path):
    module = builder()
    first = module.build(tmp_path / "a").read_bytes()
    assert first == module.build(tmp_path / "b").read_bytes()


def test_a_manifest_of_another_version_is_refused(tmp_path, monkeypatch, capsys):
    module = builder()
    monkeypatch.setattr(module, "VERSION", "0.0.1")
    assert module.main(["--output", str(tmp_path)]) == 2
    assert "make them the same" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
