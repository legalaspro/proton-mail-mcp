"""The private dev repo pays for every CI minute: macOS and the heavy runs stay behind a gate."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"
RELEASE_GATE = (
    "github.repository == 'legalaspro/proton-mail-mcp'"
    " && github.event.repository.visibility == 'public'"
)
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
    assert seen == 3


def test_the_gate_fails_closed_when_the_event_has_no_repository():
    # GitHub coerces a missing value to 0, so `repository.private == false` would be true.
    for path in WORKFLOWS.glob("*.yml"):
        assert "repository.private" not in path.read_text(encoding="utf-8"), path.name


def test_a_pull_request_runs_one_linux_and_one_windows_job():
    text = (WORKFLOWS / "check.yml").read_text(encoding="utf-8")
    assert """|| '["ubuntu-latest", "windows-latest"]') }}""" in text
    assert "pull_request" not in (WORKFLOWS / "full.yml").read_text(encoding="utf-8")
    assert "pull_request" not in (WORKFLOWS / "installer.yml").read_text(encoding="utf-8")


def test_a_full_run_starts_on_its_own_only_in_the_release_repo():
    # The push and schedule triggers fire everywhere, so these lines keep them from billing here.
    checks, installer = jobs((WORKFLOWS / "full.yml").read_text(encoding="utf-8"))
    assert f"    if: github.event_name == 'workflow_dispatch' || ({RELEASE_GATE})\n" in checks
    # The release repo's push to main already starts the installer workflow once, on its paths.
    assert (
        "    if: github.event_name == 'workflow_dispatch'"
        " || (github.ref != 'refs/heads/main' || github.event_name != 'push')"
        f" && ({RELEASE_GATE})\n"
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
    assert pins == [UV_VERSION] * 3
