"""The registry entry, the Claude Desktop bundle and the Claude Code plugin agree with the
package they describe."""

import importlib.util
import json
import os
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from proton_mail_mcp import __version__, connect

ROOT = Path(__file__).resolve().parents[1]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
PLUGIN = json.loads(
    (ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
)
PLUGIN_SERVERS = json.loads((ROOT / "plugin" / ".mcp.json").read_text(encoding="utf-8"))
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
# What the bundle and the plugin both run: this release from PyPI, by name with uvx. The plugin
# pins it with ==, the form the Claude directory's checklist gives for an exact uvx pin.
RELEASE = f"mcp-proton-mail@{__version__}"
PLUGIN_RELEASE = f"mcp-proton-mail=={__version__}"
REAL_RUN = subprocess.run


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def builder():
    return load("build_mcpb", ROOT / "scripts" / "build_mcpb.py")


def launcher():
    return load("mcpb_main", ROOT / "mcpb" / "main.py")


def fake_uvx(folder):
    folder.mkdir(parents=True)
    path = folder / ("uvx.exe" if os.name == "nt" else "uvx")
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


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


def test_the_registry_entry_the_bundle_and_the_plugin_name_the_packages_homepage():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        urls = tomllib.load(stream)["project"]["urls"]
    assert urls["Homepage"] == "https://manajev.com/proton-mail-mcp/"
    assert SERVER["websiteUrl"] == MANIFEST["homepage"] == PLUGIN["homepage"] == urls["Homepage"]
    # The bundle and the plugin each point at their own client's page in the documentation.
    assert MANIFEST["documentation"].startswith(urls["Documentation"].replace("/tree/", "/blob/"))
    assert PLUGIN["documentationUrl"].startswith(urls["Documentation"].replace("/tree/", "/blob/"))


def test_the_readme_names_the_registry_entry_for_pypi_to_prove_ownership():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    named = re.findall(r"mcp-name: ([a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+)(?=\s|-->|<|$)", readme)
    assert named == [SERVER["name"]]


def test_the_bundle_manifest_runs_this_release_with_uvx():
    # Smithery's publisher knows no "uv" server type, so the bundle is "python" and runs uvx.
    assert MANIFEST["manifest_version"] == "0.3"
    assert MANIFEST["version"] == __version__
    server = MANIFEST["server"]
    assert server["type"] == "python"
    assert server["entry_point"] == "server/main.py"
    config = server["mcp_config"]
    assert config["command"] == "uvx"
    assert config["args"] == [RELEASE, "connect", "--env-file=${user_config.env_file}"]
    assert set(MANIFEST["compatibility"]["platforms"]) == {"darwin", "linux", "win32"}
    # uvx brings Python 3.12 itself; the host's own Python is never used, so none is asked for.
    assert "runtimes" not in MANIFEST["compatibility"]
    assert f"`uvx {RELEASE} connect`" in MANIFEST["long_description"]
    assert "not affiliated with or endorsed by Proton AG" in MANIFEST["long_description"]


def test_on_macos_the_bundle_runs_the_same_command_with_more_folders_on_path():
    config = MANIFEST["server"]["mcp_config"]
    overrides = config["platform_overrides"]
    assert set(overrides) == {"darwin"}
    darwin = overrides["darwin"]
    # No env: a host would replace PATH with it, and mcpb has no variable for the PATH it had.
    assert set(darwin) == {"command", "args"}
    assert darwin["command"] == "/bin/sh"
    flag, script, name, *args = darwin["args"]
    assert (flag, name, args) == ("-c", "proton-mail-mcp", config["args"])
    # Only the setting is the host's to fill in; the shell expands $HOME and $PATH.
    assert re.findall(r"\$\{([^}:]+)\}", json.dumps(darwin)) == ["user_config.env_file"]
    # The folders go in front of the PATH the host gave, or the system's when it gave none.
    (added,) = re.findall(
        r'^export PATH="(.+):\$\{PATH:-/usr/bin:/bin:/usr/sbin:/sbin\}"; exec uvx "\$@"$', script
    )
    folders = added.split(":")
    assert folders[0] == "$HOME/.local/bin"
    assert {
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "$HOME/.nix-profile/bin",
        "/nix/var/nix/profiles/default/bin",
        "$HOME/.local/share/mise/shims",
        "$HOME/.asdf/shims",
    } <= set(folders)


def test_the_bundle_asks_only_for_the_settings_file_and_never_for_a_secret():
    assert set(MANIFEST["user_config"]) == {"env_file"}
    setting = MANIFEST["user_config"]["env_file"]
    assert setting["type"] == "file"
    assert setting["required"] is False
    assert setting["default"] == ""
    assert "sensitive" not in setting
    text = json.dumps(MANIFEST["server"])
    assert set(re.findall(r"\$\{user_config\.([^}]+)\}", text)) == set(MANIFEST["user_config"])
    assert "TOKEN" not in text.upper() and "PASSWORD" not in text.upper()


def test_an_empty_settings_file_leaves_connect_its_default(tmp_path, monkeypatch):
    # The host fills the setting in as its default, "", which leaves --env-file= empty.
    default = tmp_path / "proton-mail-mcp.env"
    monkeypatch.setattr(connect, "default_env_file", lambda environ: str(default))
    args = connect.build_parser().parse_args(["--env-file="])
    _port, _read, problem = connect.configure(args, {})
    assert f"{default} does not exist" in problem


def test_the_bundle_holds_the_manifest_the_launcher_and_the_license_only(tmp_path):
    target = builder().build(tmp_path)
    assert target.name == f"proton-mail-mcp-{__version__}.mcpb"
    with zipfile.ZipFile(target) as bundle:
        assert sorted(bundle.namelist()) == ["LICENSE", "manifest.json", "server/main.py"]
        assert json.loads(bundle.read("manifest.json")) == MANIFEST
        assert bundle.read("server/main.py") == (ROOT / "mcpb" / "main.py").read_bytes()


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


def test_a_manifest_that_runs_another_release_is_refused(tmp_path, monkeypatch, capsys):
    module = builder()
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["server"]["mcp_config"]["args"][0] = "mcp-proton-mail@0.0.1"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "manifest.json")
    assert module.main(["--output", str(tmp_path / "out")]) == 2
    assert f"make its first argument {RELEASE}" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_a_macos_command_that_runs_another_release_is_refused(tmp_path, monkeypatch, capsys):
    module = builder()
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["server"]["mcp_config"]["platform_overrides"]["darwin"]["args"][3] = "mcp-proton-mail"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "manifest.json")
    assert module.main(["--output", str(tmp_path / "out")]) == 2
    assert f"darwin override runs other arguments; end them with {RELEASE}" in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "out").exists()


def test_the_launcher_runs_the_manifest_command_with_uvx_from_uvs_folder(tmp_path):
    module = launcher()
    uvx = fake_uvx(tmp_path / ".local" / "bin")
    # Without the macOS override, so that a Mac runs the command every other system runs.
    manifest = json.loads(json.dumps(MANIFEST))
    del manifest["server"]["mcp_config"]["platform_overrides"]
    path = module.search_path({"PATH": ""}, str(tmp_path))
    words = module.command(manifest, path, ["--port", "9999"])
    assert os.path.samefile(words[0], uvx)
    assert words[1:] == [RELEASE, "connect", "--env-file=", "--port", "9999"]


def test_the_launcher_passes_the_path_it_searched_on_to_connect(tmp_path, monkeypatch):
    module = launcher()
    fake_uvx(tmp_path / ".local" / "bin")
    manifest = json.loads(json.dumps(MANIFEST))
    del manifest["server"]["mcp_config"]["platform_overrides"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "kept"))
    started = []
    monkeypatch.setattr(module.os, "execve", lambda path, words, env: started.append(env))
    monkeypatch.setattr(module.subprocess, "call", lambda words, env: started.append(env) or 0)
    assert module.main([]) == 0
    (env,) = started
    assert env["PATH"] == os.pathsep.join(
        [str(tmp_path / "kept"), str(tmp_path / ".local" / "bin")]
    )


@pytest.mark.skipif(os.name == "nt", reason="the macOS command runs /bin/sh")
@pytest.mark.parametrize("kept", ["/kept/bin:/usr/bin:/bin", ""])
def test_on_macos_uvx_and_connect_get_more_folders_in_front_of_path(tmp_path, monkeypatch, kept):
    module = launcher()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    words = module.command(MANIFEST, "/usr/bin:/bin", ["--port", "9999"])
    assert words[:2] == ["/bin/sh", "-c"]
    # In the first folder, so that no real uvx elsewhere on this computer runs instead.
    uvx = fake_uvx(tmp_path / ".local" / "bin")
    uvx.write_text('#!/bin/sh\nprintf "%s\\n" "$PATH" "$@"\n', encoding="utf-8")
    env = {"HOME": str(tmp_path), "PATH": kept}
    shown = REAL_RUN(words, env=env, capture_output=True, text=True, check=True)
    path, *args = shown.stdout.splitlines()
    assert args == [RELEASE, "connect", "--env-file=", "--port", "9999"]
    folders = path.split(":")
    assert folders[0] == str(tmp_path / ".local" / "bin")
    # Where nix, mise and asdf put uvx, which the folders a Dock app starts with leave out.
    assert {
        str(tmp_path / ".nix-profile" / "bin"),
        str(tmp_path / ".local" / "share" / "mise" / "shims"),
        str(tmp_path / ".asdf" / "shims"),
    } <= set(folders)
    # The PATH it had stays, after them; an empty one gives the system folders, not the cwd.
    assert path.endswith(":" + (kept or "/usr/bin:/bin:/usr/sbin:/sbin"))
    assert "" not in folders


def test_without_uvx_the_launcher_says_to_install_uv(tmp_path, monkeypatch, capsys):
    module = launcher()
    manifest = json.loads(json.dumps(MANIFEST))
    del manifest["server"]["mcp_config"]["platform_overrides"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "MANIFEST", tmp_path / "manifest.json")
    monkeypatch.setattr(module.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert module.main([]) == 127
    assert "install uv (https://docs.astral.sh/uv/)" in capsys.readouterr().err


def test_the_claude_code_plugin_runs_the_same_release_as_the_bundle():
    assert PLUGIN["name"] == "mcp-proton-mail"
    assert PLUGIN["version"] == __version__
    assert PLUGIN["license"] == "Apache-2.0"
    assert "not affiliated with Proton AG" in PLUGIN["description"]
    # No token, path or setting: connect finds the service and the token on its own.
    assert PLUGIN_SERVERS == {
        "mcpServers": {"proton-mail": {"command": "uvx", "args": [PLUGIN_RELEASE, "connect"]}}
    }
    assert MANIFEST["server"]["mcp_config"]["args"][:2] == [RELEASE, "connect"]
    readme = (ROOT / "plugin" / "README.md").read_text(encoding="utf-8")
    assert f"`uvx {PLUGIN_RELEASE} connect`" in readme


def test_the_plugins_lock_records_the_release_its_launcher_runs():
    with (ROOT / "plugin" / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    with (ROOT / "pyproject.toml").open("rb") as stream:
        package = tomllib.load(stream)["project"]
    # Only the requirement: a uv index or source here would be a package source of the plugin's.
    assert set(project) == {"project"}
    assert project["project"]["dependencies"] == [PLUGIN_RELEASE]
    assert project["project"]["requires-python"] == package["requires-python"]
    with (ROOT / "plugin" / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    assert lock["requires-python"] == package["requires-python"]
    locked = {entry["name"]: entry for entry in lock["package"]}
    assert locked["mcp-proton-mail"]["version"] == __version__
    pypi = {"registry": "https://pypi.org/simple"}
    # Before the upload, this release is locked against the files `uv build` wrote to dist/;
    # docs/ci.md says to refresh it from PyPI after.
    built = {"registry": "../dist"}
    for name, entry in locked.items():
        if name == project["project"]["name"]:
            assert entry["source"] == {"virtual": "."}
        elif name == "mcp-proton-mail":
            assert entry["source"] in (pypi, built)
        else:
            assert entry["source"] == pypi, name


def test_the_marketplace_lists_the_plugin_from_this_repository():
    assert MARKETPLACE["name"] == "proton-mail-mcp"
    (entry,) = MARKETPLACE["plugins"]
    assert entry["name"] == PLUGIN["name"]
    assert entry["source"] == "./plugin"
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    # Only plugin.json says the version, so the two cannot disagree.
    assert "version" not in entry


def test_a_checkout_has_no_root_mcp_json():
    # Claude Code would offer a root .mcp.json as this project's own server to anyone working
    # in the repository; the plugin's lives under plugin/.
    assert not (ROOT / ".mcp.json").exists()
