"""The private dev repo pays for every CI minute: macOS and the heavy runs stay behind a gate.

Publishing stays behind one too: only the public release repo builds and publishes a tag.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
GITHUB = WORKFLOWS.parent
DEPENDABOT = GITHUB.parent / "release" / "dependabot.yml"
RELEASE_GATE = (
    "github.repository == 'legalaspro/proton-mail-mcp'"
    " && (github.event.repository.visibility == 'public' || vars.FREE_MINUTES == 'true')"
)
PUBLISH_GATE = "github.repository == 'legalaspro/proton-mail-mcp'"
RUNNER = re.compile(r"macos-[\w.]+")
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
                else:
                    gated, fallback = line.rsplit("||", 1)
                    assert RELEASE_GATE in gated and not RUNNER.search(fallback), path.name
    assert seen == 4


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
    assert gates == 12


def test_the_dev_repo_runs_nothing_on_its_own():
    # Its gate is make check; the release repo's sync pull request runs everything, for free.
    text = (WORKFLOWS / "check.yml").read_text(encoding="utf-8")
    assert (
        "    if: (github.event_name != 'push' && github.event_name != 'pull_request')"
        f" || ({RELEASE_GATE})\n"
    ) in text
    # Linux and Windows by hand or from a Full run; a Full run reports every runner.
    assert (
        f"|| (({RELEASE_GATE}) || github.event_name == 'workflow_dispatch' || inputs.full)"
        """ && '["ubuntu-latest", "windows-latest"]' || '["ubuntu-latest"]') }}"""
    ) in text
    assert "      fail-fast: ${{ github.event_name == 'pull_request' && !inputs.full }}\n" in text
    # A Full run on a pull request also reports every failure, for one fix-forward pull request.
    maxfail = "${{ github.event_name == 'pull_request' && !inputs.full && '--maxfail=5' || '' }}"
    assert text.count(maxfail) == text.count("--maxfail") == 2
    for job in jobs((WORKFLOWS / "installer.yml").read_text(encoding="utf-8")):
        assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in job or (
            f"    if: {RELEASE_GATE}\n" in job
        ), job.splitlines()[0]


def test_a_release_pull_request_runs_the_full_and_installer_sets():
    for name in ("check.yml", "full.yml", "installer.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert '    paths-ignore:\n      - "docs/**"\n      - "**/*.md"\n      - LICENSE\n' in text
        # Only a newer push to the same pull request cancels a run: never main, a tag or a night.
        assert "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n" in text, name
        assert text.count("cancel-in-progress:") == 1, name
        if name != "check.yml":
            assert "  pull_request:\n    branches: [main]\n    paths-ignore:\n" in text, name
    full = (WORKFLOWS / "full.yml").read_text(encoding="utf-8")
    # The full matrix runs every night and on a tag, no longer on each push to main.
    assert '\non:\n  push:\n    tags: ["v*"]\n  pull_request:\n' in full
    assert '  schedule:\n    - cron: "17 3 * * *"\n' in full
    # A pull request: Linux and macOS on the oldest and newest Python; Checks adds Windows.
    assert (
        f"python: ${{{{ fromJSON(github.event_name != 'pull_request' && {RELEASE_GATE}"
        """ && '["3.12", "3.13", "3.14"]' || '["3.12", "3.14"]') }}"""
    ) in full
    assert (
        f"os: ${{{{ github.event_name == 'pull_request' && {RELEASE_GATE}"
        """ && '["ubuntu-latest", "macos-latest"]' || '' }}"""
    ) in full


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


def test_a_full_run_starts_on_its_own_only_in_the_release_repo():
    # The push, pull request and schedule triggers fire everywhere, so these lines keep them from
    # billing here.
    checks, installer = jobs((WORKFLOWS / "full.yml").read_text(encoding="utf-8"))
    assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in checks
    # A pull request starts the installer workflow itself.
    assert (
        "    if: github.event_name == 'workflow_dispatch'"
        f" || github.event_name != 'pull_request' && ({RELEASE_GATE})\n"
    ) in installer


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
    build, publish = jobs(text)
    # Each job, the build too: in the dev repo a tag starts nothing, as docs/ci.md says. A
    # release on any other tag skips the build, and with it publish, which needs it.
    assert f"    if: {PUBLISH_GATE} && startsWith(github.ref, 'refs/tags/v')\n" in build
    assert f"    if: {PUBLISH_GATE}\n" in publish
    assert "id-token" not in build and "pypi-publish" not in build
    # The build runs the build backend from the tagged tree: it keeps the read-only token.
    assert "permissions:" not in build
    assert "    needs: build\n" in publish
    assert "    environment:\n      name: pypi\n" in publish
    assert "    permissions:\n      id-token: write\n    steps:\n" in publish
    # It runs nothing from the repository with the token at hand, and PyPI needs no stored one.
    assert "actions/checkout" not in publish and "run:" not in publish
    assert "secrets." not in text
    assert publish.count("uses: pypa/gh-action-pypi-publish@") == 1
    for path in WORKFLOWS.glob("*.yml"):
        if path.name != "publish.yml":
            other = path.read_text(encoding="utf-8")
            assert "id-token" not in other and "pypi-publish" not in other, path.name


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
