"""Every file names one release of the package, the plugin trails it by one at most, and a
release's notes come from its CHANGELOG.md section (docs/ci.md, Releasing)."""

import importlib.util
import re
from pathlib import Path

import pytest

from proton_mail_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


versions = load("check_versions")
release_notes = load("release_notes")
FILES = versions.read(ROOT)
RELEASES = versions.RELEASE.findall(FILES[versions.CHANGELOG])


def with_plugin(files, version):
    """The files with every plugin file, the lock included, naming version."""
    new = dict(files)
    for where in versions.PLUGIN:
        new[where.path] = where.pattern.sub(version, new[where.path])
    (old,) = {found for _, _, found in versions.locked(new[versions.LOCK], [])}
    entry = 'name = "mcp-proton-mail"\nversion = "{}"'
    lock = new[versions.LOCK].replace(entry.format(old), entry.format(version))
    lock = lock.replace(f'specifier = "=={old}"', f'specifier = "=={version}"')
    assert {found for _, _, found in versions.locked(lock, [])} == {version}
    new[versions.LOCK] = lock
    return new


def test_this_tree_names_one_release(capsys):
    assert versions.problems(FILES) == []
    assert versions.main([]) == 0
    assert capsys.readouterr().out.startswith(f"Every file names {__version__};")


def test_every_place_the_package_names_its_version_is_checked():
    # __init__.py, server.json twice, and the bundle's version and the release its uvx runs.
    assert sum(where.count for where in versions.PACKAGE) == 7
    for where in versions.PACKAGE:
        assert len(where.pattern.findall(FILES[where.path])) == where.count, where
    assert {where.path for where in versions.PLUGIN} == {
        "plugin/.claude-plugin/plugin.json",
        "plugin/.mcp.json",
        "plugin/pyproject.toml",
        "plugin/README.md",
    }


def test_a_tag_must_be_v_and_the_packages_version(capsys):
    assert versions.main(["--tag", f"v{__version__}"]) == 0
    assert versions.main(["--tag", __version__]) == 1
    assert f"the tag is {__version__}, not v{__version__}" in capsys.readouterr().err
    assert versions.problems(FILES, tag="v0.0.1") == [
        f"the tag is v0.0.1, not v{__version__}: tag the commit that sets its version"
    ]


@pytest.mark.parametrize("index", range(len(versions.PACKAGE)))
def test_a_place_that_names_another_version_is_refused(index):
    where = versions.PACKAGE[index]
    files = dict(FILES)
    files[where.path] = where.pattern.sub("0.0.1", files[where.path], count=1)
    found = versions.problems(files)
    if where.path == versions.INIT:
        # The version lives here: every place that repeats it now disagrees with it.
        assert sum(f"is {__version__}, not 0.0.1 as in {where.path}" in line for line in found) == 6
    else:
        assert found == [
            f"{where.path}: {where.what} is 0.0.1, not {__version__} as in {versions.INIT}; "
            f"run scripts/bump_version.py {__version__}"
        ]


def test_a_file_that_names_the_version_more_or_fewer_times_is_refused():
    files = dict(FILES)
    files["server.json"] = files["server.json"].replace(
        '"name"', f'"version": "{__version__}",\n  "name"', 1
    )
    assert versions.problems(files) == [
        "server.json: names the entry's and its package's version 3 times, not 2"
    ]
    files = dict(FILES)
    files["plugin/README.md"] = re.sub(
        r"mcp-proton-mail==\S+", "mcp-proton-mail", FILES["plugin/README.md"]
    )
    assert versions.problems(files) == ["plugin/README.md: names the release uvx runs nowhere"]


def test_the_changelogs_newest_release_must_be_the_packages():
    # The plugin on the package's release, as it is between releases, so that only the heading
    # is wrong; right after a bump it still runs the release before.
    files = with_plugin(FILES, __version__)
    files["CHANGELOG.md"] = FILES["CHANGELOG.md"].replace(f"## {__version__}", "## 0.0.1", 1)
    (found,) = versions.problems(files)
    assert found.startswith(
        f"CHANGELOG.md: the newest release heading is ## 0.0.1, not ## {__version__};"
    )
    # An Unreleased section above it is no release.
    files["CHANGELOG.md"] = FILES["CHANGELOG.md"].replace(
        f"## {__version__}", f"## Unreleased\n\n- A change.\n\n## {__version__}", 1
    )
    assert versions.problems(files) == []


def test_the_plugin_may_trail_by_one_release_and_no_more():
    assert versions.problems(with_plugin(FILES, __version__)) == []
    assert versions.problems(with_plugin(FILES, RELEASES[1])) == []
    for version in (RELEASES[2], "99.0.0"):
        (found,) = versions.problems(with_plugin(FILES, version))
        assert found.startswith(
            f"the plugin runs {version}, but may run only {__version__}, or {RELEASES[1]} "
            f"until {__version__} is on PyPI"
        )
        assert "scripts/bump_version.py <version> --plugin" in found


def test_the_plugins_files_must_name_one_release():
    files = with_plugin(FILES, __version__)
    files["plugin/README.md"] = with_plugin(FILES, RELEASES[1])["plugin/README.md"]
    (found,) = versions.problems(files)
    assert found.startswith("the plugin's files name more than one release (")
    assert f"plugin/README.md {RELEASES[1]}" in found
    # The lock alone, as bump_version.py leaves it before uv writes it again.
    files = with_plugin(FILES, RELEASES[1])
    files[versions.LOCK] = with_plugin(FILES, __version__)[versions.LOCK]
    (found,) = versions.problems(files)
    assert f"plugin/uv.lock {__version__}" in found
    assert versions.problems(files, lock=False) == []


def test_a_lock_without_one_locked_or_required_release_is_refused():
    files = dict(FILES)
    files[versions.LOCK] = FILES[versions.LOCK].replace(
        'name = "mcp-proton-mail"\n', 'name = "other"\n', 1
    )
    files[versions.LOCK] = re.sub(
        r'specifier = "==[^"]+"', 'specifier = ">=1"', files[versions.LOCK]
    )
    assert versions.problems(files) == [
        "plugin/uv.lock: locks mcp-proton-mail 0 times, not once",
        "plugin/uv.lock: its project does not require one mcp-proton-mail==<version>",
    ]


def test_files_it_cannot_read_are_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(versions, "ROOT", tmp_path)
    assert versions.main([]) == 1
    assert "cannot check the versions" in capsys.readouterr().err


def test_a_releases_notes_are_its_section_without_the_heading():
    text = release_notes.notes(FILES["CHANGELOG.md"], __version__)
    assert text and text == text.strip()
    assert not re.search(r"(?m)^## ", text)
    # The section runs to the next release's heading, whose entries are not in it.
    following = release_notes.notes(FILES["CHANGELOG.md"], RELEASES[1])
    assert following and following not in text
    changelog = (
        "# Changelog\n\n## 1.2.10 - 2026-01-02\n\n- Ten.\n\n## 1.2.1\n\n### Fixed\n\n- One.\n"
    )
    assert release_notes.notes(changelog, "1.2.1") == "### Fixed\n\n- One."
    assert release_notes.notes(changelog, "1.2.10") == "- Ten."
    assert release_notes.notes(changelog, "1.2") is None
    assert release_notes.notes(changelog, "1.2.2") is None
    assert release_notes.notes("## 1.2.3 - 2026-01-02\n\n## 1.2.2\n", "1.2.3") == ""


def test_the_notes_go_to_a_file_in_utf_8(tmp_path):
    target = tmp_path / "notes.md"
    assert release_notes.main([__version__, "--output", str(target)]) == 0
    expected = release_notes.notes(FILES["CHANGELOG.md"], __version__)
    assert target.read_bytes() == f"{expected}\n".encode("utf-8")


def test_missing_or_empty_notes_are_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "CHANGELOG.md").write_bytes(b"# Changelog\n\n## 1.2.3 - 2026-01-02\n\n## 1.2.2\n")
    monkeypatch.setattr(release_notes, "ROOT", tmp_path)
    assert release_notes.main(["1.2.3"]) == 1
    assert "## 1.2.3 section is empty: write its entries" in capsys.readouterr().err
    assert release_notes.main(["1.2.4"]) == 1
    assert "CHANGELOG.md has no ## 1.2.4 section" in capsys.readouterr().err
    with pytest.raises(SystemExit) as stopped:
        release_notes.main(["v1.2.3"])
    assert stopped.value.code == 2
