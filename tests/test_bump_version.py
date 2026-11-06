"""scripts/bump_version.py sets a release everywhere the package or the plugin names it, and
changes nothing it cannot check (docs/ci.md, Releasing). Each test works on a copy of the files;
uv is a stand-in, so nothing reaches PyPI."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from proton_mail_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-11-04"
MAJOR, MINOR, PATCH = (int(part) for part in __version__.split("."))
NEXT = f"{MAJOR}.{MINOR}.{PATCH + 1}"
AFTER = f"{MAJOR}.{MINOR + 1}.0"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(root):
    return {path: (root / path).read_bytes() for path in load("check_versions").FILES}


@pytest.fixture
def bump(tmp_path, monkeypatch):
    """The script, working on a copy of the files with the plugin on the package's release."""
    module = load("bump_version")
    for path in module.versions.FILES:
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    files = module.versions.read(tmp_path)
    plugin = module.versions.PLUGIN[0].pattern.findall(files[module.versions.PLUGIN[0].path])[0]
    if plugin != __version__:
        relock(tmp_path / "plugin", plugin, __version__)
        for where in module.versions.PLUGIN:
            text = where.pattern.sub(__version__, files[where.path])
            (tmp_path / where.path).write_bytes(text.encode("utf-8"))
    # Each test adds its own Unreleased section, or none.
    changelog = files["CHANGELOG.md"]
    unreleased = module.UNRELEASED.search(changelog)
    if unreleased:
        newest = module.versions.RELEASE.search(changelog)
        changelog = changelog[: unreleased.start()] + changelog[newest.start() :]
        (tmp_path / "CHANGELOG.md").write_bytes(changelog.encode("utf-8"))
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/stand-in/{name}")
    assert module.versions.problems(module.versions.read(tmp_path)) == []
    return module


def relock(plugin, old, new, source=None):
    """What uv lock writes for new: the entry and the requirement, with PyPI's file names."""
    lock = plugin / "uv.lock"
    text = lock.read_bytes().decode("utf-8")
    entry = 'name = "mcp-proton-mail"\nversion = "{}"'
    text = text.replace(entry.format(old), entry.format(new))
    text = text.replace(f'specifier = "=={old}"', f'specifier = "=={new}"')
    text = text.replace(f"mcp_proton_mail-{old}", f"mcp_proton_mail-{new}")
    if source:
        marker = entry.format(new) + '\nsource = { registry = "https://pypi.org/simple" }'
        text = text.replace(marker, entry.format(new) + f"\nsource = {source}")
    lock.write_bytes(text.encode("utf-8"))


def stand_in_uv(monkeypatch, bump, returncode=0, source=None):
    """subprocess.run as uv lock: each call recorded, the lock moved to the plugin's release."""
    calls = []

    def run(args, cwd):
        calls.append((args, Path(cwd)))
        if returncode == 0:
            files = bump.versions.read(bump.ROOT)
            wanted = bump.versions.PLUGIN[0].pattern.findall(files[bump.versions.PLUGIN[0].path])
            (old,) = {found for _, _, found in bump.versions.locked(files[bump.versions.LOCK], [])}
            relock(Path(cwd), old, wanted[0], source)
        return subprocess.CompletedProcess(args, returncode)

    monkeypatch.setattr(bump.subprocess, "run", run)
    return calls


def test_a_new_version_goes_everywhere_the_package_names_it(bump, capsys):
    before = snapshot(bump.ROOT)
    assert bump.main([NEXT, "--date", DATE]) == 0
    files = bump.versions.read(bump.ROOT)
    assert bump.versions.problems(files) == []
    for where in bump.versions.PACKAGE:
        assert where.pattern.findall(files[where.path]) == [NEXT] * where.count, where
    # A heading with no entries, above the release before; the plugin stays on that one.
    assert f"## {NEXT} - {DATE}\n\n## {__version__}" in files["CHANGELOG.md"]
    assert bump.versions.RELEASE.findall(files["CHANGELOG.md"])[:2] == [NEXT, __version__]
    after = snapshot(bump.ROOT)
    assert {path for path in after if after[path] != before[path]} == {
        "CHANGELOG.md",
        "mcpb/manifest.json",
        "server.json",
        "src/proton_mail_mcp/__init__.py",
    }
    out = capsys.readouterr().out
    assert f"Write the entries under ## {NEXT} in CHANGELOG.md" in out
    assert f"run scripts/bump_version.py {NEXT} --plugin" in out


def test_the_unreleased_section_becomes_the_release(bump, capsys):
    changelog = bump.ROOT / "CHANGELOG.md"
    text = changelog.read_bytes().decode("utf-8")
    entries = "### Fixed\n\n- **A fix.** Described.\n\n"
    text = text.replace(f"## {__version__}", f"## Unreleased\n\n{entries}## {__version__}", 1)
    changelog.write_bytes(text.encode("utf-8"))
    assert bump.main([AFTER, "--date", DATE]) == 0
    new = changelog.read_bytes().decode("utf-8")
    # No Unreleased heading is left; an entry's text may still name the section.
    assert not bump.UNRELEASED.search(new)
    assert f"## {AFTER} - {DATE}\n\n{entries}## {__version__}" in new
    assert bump.release_notes.notes(new, AFTER) == entries.strip()
    assert "Write the entries" not in capsys.readouterr().out


def test_a_dry_run_shows_the_diff_and_writes_nothing(bump, capsys):
    before = snapshot(bump.ROOT)
    assert bump.main([NEXT, "--dry-run", "--date", DATE]) == 0
    assert snapshot(bump.ROOT) == before
    out = capsys.readouterr().out
    assert f'-__version__ = "{__version__}"\n+__version__ = "{NEXT}"\n' in out
    assert f"+## {NEXT} - {DATE}\n" in out
    assert "--- a/server.json\n+++ b/server.json\n" in out
    assert out.endswith("Dry run: nothing was written.\n")


def test_an_older_or_malformed_version_is_refused(bump, capsys):
    before = snapshot(bump.ROOT)
    older = f"{MAJOR - 1}.0.0"
    assert bump.main([older]) == 1
    assert f"{older} is older than the package's {__version__}" in capsys.readouterr().err
    for bad in (f"v{NEXT}", f"{MAJOR}.{MINOR}", f"{NEXT}rc1", f"{NEXT}.1"):
        with pytest.raises(SystemExit) as stopped:
            bump.main([bad])
        assert stopped.value.code == 2
    assert snapshot(bump.ROOT) == before


def test_the_same_version_changes_nothing(bump, capsys):
    before = snapshot(bump.ROOT)
    assert bump.main([__version__]) == 0
    assert bump.main([__version__, "--plugin"]) == 0
    assert snapshot(bump.ROOT) == before
    assert capsys.readouterr().out.count("nothing to change") == 2


def test_a_bump_that_would_leave_the_plugin_two_releases_behind_is_refused(bump, capsys):
    assert bump.main([NEXT, "--date", DATE]) == 0
    before = snapshot(bump.ROOT)
    assert bump.main([AFTER, "--date", DATE]) == 1
    err = capsys.readouterr().err
    assert f"the plugin runs {__version__}, but may run only {AFTER}, or {NEXT}" in err
    assert snapshot(bump.ROOT) == before


def test_after_the_upload_the_plugin_moves_and_uv_locks_it(bump, monkeypatch, capsys):
    calls = stand_in_uv(monkeypatch, bump)
    assert bump.main([NEXT, "--date", DATE]) == 0
    package = snapshot(bump.ROOT)
    assert bump.main([NEXT, "--plugin"]) == 0
    assert calls == [
        (["/stand-in/uv", "lock", "--upgrade-package", "mcp-proton-mail"], bump.ROOT / "plugin")
    ]
    files = bump.versions.read(bump.ROOT)
    assert bump.versions.problems(files) == []
    assert bump.lock_guard.problems(bump.ROOT / "plugin") == []
    for where in bump.versions.PLUGIN:
        assert where.pattern.findall(files[where.path]) == [NEXT] * (where.count or 1), where
    assert {found for _, _, found in bump.versions.locked(files["plugin/uv.lock"], [])} == {NEXT}
    # The package's files and the changelog are as the bump left them.
    after = snapshot(bump.ROOT)
    assert {path for path in after if after[path] != package[path]} == {
        "plugin/.claude-plugin/plugin.json",
        "plugin/.mcp.json",
        "plugin/README.md",
        "plugin/pyproject.toml",
        "plugin/uv.lock",
    }
    assert "plugin/uv.lock." in capsys.readouterr().out


def test_a_plugin_two_releases_behind_moves_to_the_release_before_the_package(bump, monkeypatch):
    assert bump.main([NEXT, "--date", DATE]) == 0
    files = bump.versions.read(bump.ROOT)
    older = bump.versions.RELEASE.findall(files["CHANGELOG.md"])[2]
    for where in bump.versions.PLUGIN:
        text = where.pattern.sub(older, files[where.path])
        (bump.ROOT / where.path).write_bytes(text.encode("utf-8"))
    relock(bump.ROOT / "plugin", __version__, older)
    assert bump.versions.problems(bump.versions.read(bump.ROOT))
    calls = stand_in_uv(monkeypatch, bump)
    assert bump.main([__version__, "--plugin"]) == 0
    assert len(calls) == 1
    assert bump.versions.problems(bump.versions.read(bump.ROOT)) == []


def test_a_plugin_ahead_of_the_package_is_refused_before_uv_runs(bump, monkeypatch, capsys):
    calls = stand_in_uv(monkeypatch, bump)
    before = snapshot(bump.ROOT)
    assert bump.main([NEXT, "--plugin"]) == 1
    assert calls == []
    assert snapshot(bump.ROOT) == before
    assert f"the plugin runs {NEXT}, but may run only {__version__}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("returncode", "source", "expected"),
    [
        (1, None, "uv lock exited with 1"),
        (0, '{ registry = "../dist" }', 'mcp-proton-mail: source { registry = "../dist" }'),
    ],
    ids=["not-on-pypi", "local-files"],
)
def test_a_lock_uv_cannot_write_from_pypi_puts_every_file_back(
    bump, monkeypatch, capsys, returncode, source, expected
):
    assert bump.main([NEXT, "--date", DATE]) == 0
    before = snapshot(bump.ROOT)
    calls = stand_in_uv(monkeypatch, bump, returncode, source)
    assert bump.main([NEXT, "--plugin"]) == 1
    assert len(calls) == 1
    assert snapshot(bump.ROOT) == before
    err = capsys.readouterr().err
    assert expected in err
    assert f"every file is as it was; plugin/uv.lock can lock {NEXT} only once it is on PyPI" in err


def test_ctrl_c_while_uv_locks_puts_every_file_back(bump, monkeypatch):
    assert bump.main([NEXT, "--date", DATE]) == 0
    before = snapshot(bump.ROOT)
    calls = stand_in_uv(monkeypatch, bump)
    locking = bump.subprocess.run

    def interrupted(args, cwd):
        locking(args, cwd)
        raise KeyboardInterrupt

    monkeypatch.setattr(bump.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bump.main([NEXT, "--plugin"])
    assert len(calls) == 1
    assert snapshot(bump.ROOT) == before


@pytest.mark.parametrize("always", [False, True], ids=["once", "always"])
def test_a_file_that_cannot_be_written_puts_every_file_back(bump, monkeypatch, capsys, always):
    # Files are written in order, so CHANGELOG.md has its new heading when server.json fails.
    before = snapshot(bump.ROOT)
    real = Path.write_bytes

    def write_bytes(path, data):
        if path == bump.ROOT / "server.json" and (always or data != before["server.json"]):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    assert bump.main([NEXT, "--date", DATE]) == 1
    assert snapshot(bump.ROOT) == before
    err = capsys.readouterr().err
    assert "cannot write the files: [Errno 13] Permission denied" in err
    if always:
        assert "Could not put back server.json: restore them with git." in err
        assert "Not every file is as it was:" in err
    else:
        assert "Could not put back" not in err
        assert "every file is as it was\n" in err


def test_the_plugin_needs_uv_and_a_dry_run_does_not_run_it(bump, monkeypatch, capsys):
    calls = stand_in_uv(monkeypatch, bump)
    assert bump.main([NEXT, "--date", DATE]) == 0
    before = snapshot(bump.ROOT)
    assert bump.main([NEXT, "--plugin", "--dry-run"]) == 0
    assert calls == []
    assert snapshot(bump.ROOT) == before
    out = capsys.readouterr().out
    assert f'+dependencies = ["mcp-proton-mail=={NEXT}"]\n' in out
    assert "Then, in plugin/: uv lock --upgrade-package mcp-proton-mail\n" in out
    monkeypatch.setattr(bump.shutil, "which", lambda name: None)
    assert bump.main([NEXT, "--plugin"]) == 1
    assert calls == []
    assert snapshot(bump.ROOT) == before
    assert "--plugin runs uv, which is not on PATH" in capsys.readouterr().err
