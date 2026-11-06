"""The private dev repo pays for every CI minute: macOS and the heavy runs stay behind a gate.

Publishing stays behind one too: only the public release repo builds and publishes a tag.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
GITHUB = WORKFLOWS.parent
DEPENDABOT = GITHUB.parent / "release" / "dependabot.yml"
PUBLISH = WORKFLOWS / "publish.yml"
RELEASE_GATE = (
    "github.repository == 'legalaspro/proton-mail-mcp'"
    " && (github.event.repository.visibility == 'public' || vars.FREE_MINUTES == 'true')"
)
PUBLISH_GATE = "github.repository == 'legalaspro/proton-mail-mcp'"
RUNNER = re.compile(r"macos-[\w.]+")
LIST = re.compile(r"'\[[^']*\]'")
UV_VERSION = "0.13.6"


def jobs(text):
    """Each job's lines; workflows are written with two-space job keys."""
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    return re.split(r"(?m)^(?=  [\w-]+:$)", body)[1:]


def test_every_macos_runner_is_behind_the_release_gate():
    seen = 0
    for path in WORKFLOWS.glob("*.yml"):
        for job in jobs(path.read_text(encoding="utf-8")):
            for line in job.splitlines():
                if not RUNNER.search(line):
                    continue
                seen += 1
                if "runs-on:" in line:
                    assert f"    if: {RELEASE_GATE}\n" in job, path.name
                    continue
                # Each list of runners that names macOS has the gate in its own condition, the
                # text since the list before it; the last list, the fallback, never names macOS.
                start = 0
                lists = list(LIST.finditer(line))
                for runners in lists:
                    if RUNNER.search(runners.group()):
                        assert RELEASE_GATE in line[start : runners.start()], path.name
                    start = runners.end()
                assert not RUNNER.search(lists[-1].group()), path.name
    assert seen == 3


def test_the_gate_fails_closed_when_the_event_has_no_repository():
    # GitHub coerces a missing value to 0, so `repository.private == false` would be true.
    for path in WORKFLOWS.glob("*.yml"):
        assert "repository.private" not in path.read_text(encoding="utf-8"), path.name


def test_every_gate_has_the_release_repos_fallback_for_an_event_without_visibility():
    # A schedule event may carry no repository, and a nightly that skips looks green. The release
    # repo sets FREE_MINUTES instead; the name check stays outside it, so no variable set in this
    # repo or a fork opens the gate.
    gates = 0
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert text.count("repository.visibility") == text.count(RELEASE_GATE), path.name
        assert text.count("vars.") == text.count(RELEASE_GATE), path.name
        gates += text.count(RELEASE_GATE)
    assert gates == 11


def test_the_dev_repo_runs_nothing_on_its_own():
    # Its gate is make check; the release repo's sync pull request runs everything, for free.
    text = (WORKFLOWS / "check.yml").read_text(encoding="utf-8")
    assert (
        "    if: (github.event_name != 'push' && github.event_name != 'pull_request')"
        f" || ({RELEASE_GATE})\n"
    ) in text
    # Windows only with full or by hand; macOS only behind the gate; the dev repo's fallback is
    # Linux alone.
    assert (
        f"        os: ${{{{ fromJSON(inputs.full && {RELEASE_GATE}"
        """ && '["ubuntu-latest", "windows-latest", "macos-latest"]'"""
        " || (github.event_name == 'workflow_dispatch' || inputs.full)"
        """ && '["ubuntu-latest", "windows-latest"]'"""
        f""" || ({RELEASE_GATE}) && '["ubuntu-latest", "macos-latest"]'"""
        """ || '["ubuntu-latest"]') }}\n"""
    ) in text
    # Every runner reports every failure, on a pull request too: no Full run reports them there.
    assert "      fail-fast: false\n" in text and text.count("fail-fast:") == 1
    # On a pull request pytest stops after 5 failures in each job; a Full run reports them all.
    maxfail = "${{ github.event_name == 'pull_request' && !inputs.full && '--maxfail=5' || '' }}"
    assert text.count(maxfail) == text.count("--maxfail") == 2
    for job in jobs((WORKFLOWS / "installer.yml").read_text(encoding="utf-8")):
        assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in job or (
            f"    if: {RELEASE_GATE}\n" in job
        ), job.splitlines()[0]


def test_a_release_pull_request_runs_the_lean_set():
    for name in ("check.yml", "full.yml", "installer.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        # Only a newer push to the same pull request cancels a run. Every other run has a group
        # of its own, so main, a tag, the nightly or a run by hand never cancels or waits.
        assert "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n" in text, name
        assert text.count("cancel-in-progress:") == 1, name
        (group,) = re.findall(r"(?m)^  group: (.*)$", text)
        assert group.endswith(
            "${{ github.event_name == 'pull_request'"
            " && github.event.pull_request.number || github.run_id }}"
        ), name
    check = (WORKFLOWS / "check.yml").read_text(encoding="utf-8")
    assert (
        '  pull_request:\n    paths-ignore:\n      - "docs/**"\n      - "**/*.md"\n'
        "      - LICENSE\n"
    ) in check
    # Linux and macOS on 3.12 (the os line is in the test above), and Linux on 3.14, only on a
    # pull request or a push to main; a Full run passes its own Python.
    assert """        python: ["${{ inputs.python-version || '3.12' }}"]\n""" in check
    assert (
        "        include: ${{ fromJSON((github.event_name == 'pull_request'"
        " || github.event_name == 'push') && !inputs.full"
        """ && '[{"os":"ubuntu-latest","python":"3.14"}]' || '[]') }}\n"""
    ) in check
    assert check.count("matrix.python }}") == 3 and "inputs.os" not in check
    # The slow tests only with full: every night, on a tag, or by hand.
    assert check.count("""${{ !inputs.full && '-m "not slow"' || '' }}""") == 2
    assert check.count("not slow") == 2
    # The whole tree's lint, and the pull request's one macOS job runs the wheel smoke.
    lint = "      - if: runner.os == 'Linux'\n        run: ruff check . && ruff format --check .\n"
    assert lint in check
    assert (
        "      - if: runner.os == 'macOS' && !inputs.full\n"
        "        run: python scripts/wheel_smoke.py --python ${{ matrix.python }}\n"
    ) in check
    full = (WORKFLOWS / "full.yml").read_text(encoding="utf-8")
    # The full matrix runs every night, on a tag and by hand, never on a pull request.
    assert (
        '\non:\n  push:\n    tags: ["v*"]\n  schedule:\n    - cron: "17 3 * * *"\n'
        "  workflow_dispatch:\n\n"
    ) in full
    assert "pull_request:" not in full and "os:" not in full
    assert (
        f"python: ${{{{ fromJSON({RELEASE_GATE}"
        """ && '["3.12", "3.13", "3.14"]' || '["3.12", "3.14"]') }}"""
    ) in full
    # The installer runs on a pull request only when its own paths change, and no longer on a
    # push to main.
    installer = (WORKFLOWS / "installer.yml").read_text(encoding="utf-8")
    on = installer[installer.index("\non:\n") : installer.index("\npermissions:\n")]
    assert on.startswith("\non:\n  pull_request:\n    branches: [main]\n    paths:\n")
    assert on.endswith("\n  workflow_dispatch:\n  workflow_call:\n")
    assert "push:" not in on and "paths-ignore" not in on
    paths = re.findall(r"(?m)^      - (.+)$", on)
    for path in (
        '"scripts/install*"',
        "scripts/wheel_smoke.py",
        '"src/proton_mail_mcp/installer/**"',
        # Imported by the installer, or driven by the wheel smoke.
        "src/proton_mail_mcp/config.py",
        "src/proton_mail_mcp/connect.py",
        "src/proton_mail_mcp/localfs.py",
        '"tests/test_installer_*.py"',
        ".github/workflows/installer.yml",
    ):
        assert path in paths, path


def test_every_checks_job_first_refuses_a_plugin_lock_that_names_anything_but_pypi():
    # So a sync pull request with such a lock fails a required check by name, before the tests;
    # the Claude directory refuses that lock, and the sync runs the same script before it pushes.
    (job,) = jobs((WORKFLOWS / "check.yml").read_text(encoding="utf-8"))
    step = (
        "      - name: The plugin's lock names only PyPI\n"
        "        run: python scripts/check_plugin_lock.py\n"
    )
    assert job.count(step) == 1
    assert job.index("actions/setup-python@") < job.index(step) < job.index("uv pip install")
    assert (GITHUB.parent / "scripts" / "check_plugin_lock.py").is_file()


def test_every_checks_job_checks_that_the_versions_agree_before_the_tests():
    # The same check Publish runs with the tag: a sync pull request whose files name different
    # releases, or whose plugin trails by more than one, fails a required check by name.
    (job,) = jobs((WORKFLOWS / "check.yml").read_text(encoding="utf-8"))
    step = "      - name: The versions agree\n        run: python scripts/check_versions.py\n"
    assert job.count(step) == 1
    assert job.index("actions/setup-python@") < job.index(step) < job.index("uv pip install")


def test_a_full_run_starts_on_its_own_only_in_the_release_repo():
    # The tag and schedule triggers fire everywhere, so these lines keep them from billing here.
    checks, installer = jobs((WORKFLOWS / "full.yml").read_text(encoding="utf-8"))
    assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in checks
    assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in installer


def test_uv_is_pinned_to_one_version_and_setup_uv_to_a_commit():
    pins = []
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        steps = re.findall(r"astral-sh/setup-uv@(\S+).*\n\s+with:\n\s+version: \"(\S+)\"", text)
        assert len(steps) == text.count("astral-sh/setup-uv@"), path.name
        for commit, version in steps:
            assert re.fullmatch(r"[0-9a-f]{40}", commit), path.name
            pins.append(version)
        assert len(re.findall(r"pip install uv\b", text)) == text.count("pip install uv=="), (
            path.name
        )
        pins += re.findall(r"pip install uv==(\S+)", text)
    assert pins == [UV_VERSION] * 4


def test_every_action_is_pinned_to_a_commit():
    # A tag can move, and the public repo runs these on pull requests from forks.
    for path in WORKFLOWS.glob("*.yml"):
        for action in re.findall(r"uses: (\S+)", path.read_text(encoding="utf-8")):
            pinned = re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action)
            assert pinned or action.startswith("./"), (path.name, action)


def test_every_workflow_reads_only_and_every_checkout_drops_its_credentials():
    # Fork pull requests run these in the public repo; a job that needs more asks for it itself.
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "\npermissions:\n  contents: read\n\n" in text, path.name
        checkouts = text.count("uses: actions/checkout@")
        assert text.count("persist-credentials: false") == checkouts, path.name


def test_only_the_release_repo_publishes_and_only_a_tag():
    text = (WORKFLOWS / "publish.yml").read_text(encoding="utf-8")
    assert '\non:\n  push:\n    tags: ["v*"]\n  release:\n    types: [published]\n\n' in text
    # A tag's run and its release's run wait for each other; neither is ever cancelled.
    assert (
        "\nconcurrency:\n  group: publish-${{ github.ref }}\n  cancel-in-progress: false\n" in text
    )
    build, publish, release, registry = jobs(text)
    # Each job, the build too: in the dev repo a tag starts nothing, as docs/ci.md says. A
    # release on any other tag skips the build, and with it every job, as each needs it.
    assert f"    if: {PUBLISH_GATE} && startsWith(github.ref, 'refs/tags/v')\n" in build
    for job in (publish, release, registry):
        assert f"    if: {PUBLISH_GATE}\n" in job
    assert "id-token" not in build and "pypi-publish" not in build
    # The build runs the build backend from the tagged tree: it keeps the read-only token. It
    # checks every version against the tag, and the notes, before anything is uploaded.
    assert "permissions:" not in build
    assert 'python scripts/check_versions.py --tag "$TAG"\n' in build
    assert "python scripts/release_notes.py " in build
    assert build.index("scripts/release_notes.py") < build.index("upload-artifact@")
    assert "    needs: build\n" in publish
    assert "    environment:\n      name: pypi\n" in publish
    assert "    permissions:\n      id-token: write\n    steps:\n" in publish
    # It runs nothing from the repository with the token at hand, and PyPI needs no stored one.
    assert "actions/checkout" not in publish and "run:" not in publish
    assert "secrets." not in text
    assert publish.count("uses: pypa/gh-action-pypi-publish@") == 1
    # Only the pypi environment holds an upload for its reviewer; nothing else names one.
    assert text.count("environment:") == 1
    for job in (release, registry):
        # Both wait for the upload, so neither runs before the reviewer approves it, and each
        # checks nothing out: what they need comes from the build.
        assert "    needs: [build, publish]\n" in job
        assert "actions/checkout" not in job and "pypi-publish" not in job
        assert "environment:" not in job
    # The release may write releases and nothing else, and leaves one that exists alone.
    assert "    permissions:\n      contents: write\n    steps:\n" in release
    assert "id-token" not in release
    assert "GH_TOKEN: ${{ github.token }}\n" in release
    assert 'gh release view "$TAG"' in release and "gh release create" in release
    assert "--notes-file release-files/notes.md" in release
    # The registry gets only the OIDC token, and no repository content, and logs in with it.
    assert "    permissions:\n      id-token: write\n    env:\n" in registry
    assert "./mcp-publisher login github-oidc\n" in registry
    assert "./mcp-publisher publish release-files/server.json\n" in registry
    assert re.search(r"(?m)login github$", registry) is None
    for job in (build, publish, release):
        assert "mcp-publisher" not in job
    for path in WORKFLOWS.glob("*.yml"):
        if path.name != "publish.yml":
            other = path.read_text(encoding="utf-8")
            assert "id-token" not in other and "pypi-publish" not in other, path.name
            assert "mcp-publisher" not in other and "contents: write" not in other, path.name


def test_mcp_publisher_is_pinned_to_a_version_and_its_hash():
    # A downloaded program, not an action pinned to a commit: the job refuses any other bytes.
    (registry,) = [job for job in jobs(PUBLISH.read_text(encoding="utf-8")) if "registry:" in job]
    (version,) = re.findall(r'(?m)^      MCP_PUBLISHER_VERSION: "(\d+\.\d+\.\d+)"$', registry)
    (digest,) = re.findall(r"(?m)^      MCP_PUBLISHER_SHA256: ([0-9a-f]{64})$", registry)
    assert version and digest
    assert "/v$MCP_PUBLISHER_VERSION/mcp-publisher_linux_amd64.tar.gz" in registry
    check = 'echo "$MCP_PUBLISHER_SHA256  mcp-publisher.tar.gz" | sha256sum --check --strict\n'
    assert check in registry
    # The check comes before the archive is opened, and nothing runs it before then.
    assert registry.index(check) < registry.index("tar -xzf") < registry.index("./mcp-publisher")
    assert "releases/latest" not in registry


def test_dependabot_opens_pull_requests_only_in_the_release_repo():
    # Its pull requests get CI only in the release repo, so the config waits in release/, where
    # Dependabot does not look, and the release sync moves it into .github there. A tree holds
    # exactly one.
    found = [
        path
        for path in (DEPENDABOT, GITHUB / "dependabot.yml", GITHUB / "dependabot.yaml")
        if path.exists()
    ]
    assert len(found) == 1, found
    head, *updates = re.split(r"(?m)^  - ", found[0].read_text(encoding="utf-8"))
    assert re.fullmatch(r"(#.*\n)*version: 2\nupdates:\n", head)
    ecosystems = []
    for update in updates:
        first, *rest = update.splitlines()
        ecosystems.append(first.removeprefix("package-ecosystem: "))
        settings = dict(re.fullmatch(r"    ([\w-]+): (\S.*)", line).groups() for line in rest)
        assert settings.keys() <= {
            "directory",
            "schedule",
            "cooldown",
            "versioning-strategy",
            "open-pull-requests-limit",
            "groups",
        }
        assert settings["directory"] == "/"
        assert settings["schedule"] == "{interval: weekly}"
        assert settings["cooldown"] == "{default-days: 7}"
    assert ecosystems == ["pip", "github-actions"]
