# CI

What GitHub Actions runs, where and at what cost, and how a release reaches PyPI. The same
workflow files run in a private dev repo, where every minute is billed, and in the public release
repo: they stay cheap in the first and full in the second, and only the second publishes.

## Two repos, one set of workflows

| | Private dev repo | Public `legalaspro/proton-mail-mcp` |
| --- | --- | --- |
| Minutes | billed | free on hosted runners |
| Pull request | one Linux and one Windows job, Python 3.12, `not slow` | the same |
| Push to `main` | Linux with slow tests, Windows `not slow`; the installer workflow if its paths changed | the same, and a Full run |
| `v*` tag | nothing | Full run with the installer workflow, and [Publish](#publishing-to-pypi) |
| Published GitHub release | nothing | [Publish](#publishing-to-pypi) |
| Every Monday | nothing | Full run with the installer workflow |
| Full run | by hand only | also by hand |
| Full run covers | Linux and Windows, Python 3.12 and 3.14 | Linux, Windows and macOS, Python 3.12, 3.13 and 3.14 |
| macOS | **never** | in every Full run and the installer workflow |
| Dependabot | **never** | weekly pull requests for the Python dependencies and the actions |

**No macOS job can run in the private dev repo.** Each macOS runner sits behind this condition:

```yaml
github.repository == 'legalaspro/proton-mail-mcp' && github.event.repository.visibility == 'public'
```

It needs both parts, so it stays false in the dev repo, in forks, and in the release repo whenever
that repo is not public. Where it is false, test macOS by hand (see [install.md](install.md)). The
public Full run of 2 Nov 2026 ran macOS on Python 3.12, 3.13 and 3.14: two tests failed there, both
fixed since in the tests themselves, and the fixes have not run on macOS yet.

Don't write this as `github.event.repository.private == false`: GitHub converts a missing value to
0, and false to 0, so that is true on any event with no `repository`. A string comparison fails
closed. `tests/test_ci_workflows.py` holds every workflow to this.

This condition first ran in the public repo on 2 Nov 2026, in a Full run started by hand, and the
macOS jobs ran. On the first Monday, tag or `main` push there, check that they ran rather than
showing as skipped. GitHub's documents don't confirm that
`visibility` is present on a `schedule` event; if it isn't, the Monday run skips without warning,
though it can't overspend. The release repo also needs these workflow files: this repo's pull
requests do not put them there.

A run whose jobs are all skipped bills nothing, so the Full run entries that appear here on each
push to `main` and every Monday cost nothing.

**Dependabot runs only in the release repo.** Each of its pull requests would start a billed run
here (35-44 minutes, see [Costs](#costs)), so this repo keeps its config at
`release/dependabot.yml`, where Dependabot does not look. The release sync moves that file to
`.github/dependabot.yml` in the release tree, unchanged, and leaves no `release/dependabot.yml`
there. `tests/test_ci_workflows.py` fails on a tree that has both files or neither. Each sync
replaces the release tree, so make an update that Dependabot proposes in this repo: one merged only
in the release repo is lost at the next sync.

## What runs when in this repo

| Event | Linux | Windows | Other Pythons | Installer workflow |
| --- | --- | --- | --- | --- |
| Pull request | `not slow`, dry runs | `not slow`, dry runs | no | no |
| Docs-only pull request | nothing | nothing | no | no |
| Push to `main` | all tests, dry runs | `not slow`, dry runs | no | only if its paths changed |
| **Checks** by hand | `not slow`, or all with `full` | `not slow`, or all with `full`, dry runs | the one you name | no |
| **Installer** by hand | no | no | no | yes |
| **Full run** by hand | all tests, dry runs | all tests, dry runs | 3.12 and 3.14 | yes |

New pushes to a pull request cancel its older runs, the first failing job cancels the other, and
pytest stops after 5 failures. Pushes to `main` and Full runs never cancel and report everything.

The job's dry runs are `shellcheck`, `install.sh` and `install.ps1` under the service preflight.
They take seconds, so they run on every pull request instead of only when a path list matches.
The installer workflow holds what takes longer: the wheel smoke and the three Linux containers,
plus macOS in the release repo. It runs once per push, and never on a pull request's push. If you
change the installer, run `make wheel-smoke`, or run **Installer** by hand on the branch. A change
outside its paths that breaks the wheel is found by a Full run, so do one before a release.

## Where each test and platform runs

| Coverage | On a pull request | Elsewhere |
| --- | --- | --- |
| Linux, 3.12, `not slow` | yes | |
| Linux, 3.12, `slow` | no | every push to `main` |
| Windows, 3.12, `not slow` | yes | |
| Windows, `slow` | no | Full run |
| Python 3.14, Linux and Windows | no | Full run |
| Python 3.13, Linux and Windows | no | Full run in the release repo, or **Checks** by hand |
| macOS, every Python, all tests | no | Full run in the release repo only |
| Wheel smoke on Linux; Fedora, AlmaLinux and Arch dry runs | no | `main` if paths changed, **Installer** or Full run |
| Wheel smoke and dry runs on macOS, bash 3.2 | no | release repo only |

## The test selections

- **`slow`** marks cost and latency regressions on large simulated mailboxes (thousands of
  messages), and the largest size of a test that also runs smaller. **Never mark a test of tokens,
  sending, typed OK or confirmation, secrets, file permissions or the credential store as
  `slow`.** Those stay in every pull request.
- **`real_fsync`** keeps real disk flushes. Elsewhere on Linux and macOS, `tests/conftest.py`
  replaces `os.fsync` with `os.fstat`: the call still happens, tests that make it fail still see
  it, and a bad descriptor still fails, but nothing waits for the disk. The tests of the action
  log, crash recovery, the rules store and the timer keep it real. Windows always flushes for
  real, since its flushes fail in ways of their own.

`tests/conftest.py` also caches the standard library's header parser and tunes the garbage
collector. Neither changes what is tested, and neither reaches production. Rarer collections do
keep an unclosed SQLite connection open for longer, and Windows then refuses to delete its file, so
tests must close their connections; `test_windows_compat.py` checks that they do.

## Production code this work changed

CI spends most of its time in sender checks, so this work also made three production modules
cheaper. A fuzz against `main` gave the same answers on 580,000 hostile cases (**measured**, local,
Python 3.12): `ProtectedPolicy.evaluate` 120,000, `sender_matches` 60,000, `_ImapValues` 200,000,
and `sender_snapshot` with header-object matching 200,000.

- **`protected.py`, the safety gate:** `ProtectedPolicy.evaluate` caches the parse of each sender
  and the match of a domain or name against the rule lists. The lists are part of the key, so a
  changed rule applies at once. A bypass record and the reasons are still built on every call.
- **`sender_matching.py`:** caches the parse of a sender as its text, its addresses and one flag
  for defects or a named group, never a header object.
- **`mail.py`:** `_ImapValues` scans with regular expressions instead of byte loops. It has no cache.

The server is long-lived and mail chooses these strings, so each of the four caches holds at most
16,384 entries, and a string over 256 characters is parsed every time and never kept.

| All four caches full | Memory | |
| --- | --- | --- |
| Typical senders, before the limits: 32,768 whole header objects, no length limit | 631 MiB | measured |
| Typical senders, now | 23 MiB | measured |
| Costliest shape found, now: 16,384 senders of 128 one-character non-ASCII names, such as `𠀀,𠀀` | 319 MiB | measured |
| Costliest valid addresses found, now: 16,384 senders of 42 addresses, such as `a@𠀀.𠀀` | 271 MiB | measured |
| Upper limit that `tests/test_sender_parse_cache.py` asserts | 544 MiB | computed from object sizes |

The memory figures are from Python 3.12. The check stops at the first bare name, so the domain and
name caches stay empty, but the text cache keeps all 128. The test adds each cache's costliest shape
separately, which no single shape reaches, and gets 498 MiB on Python 3.12 and 3.13 and 520 MiB on
3.14, where a tuple is larger. A real peak could sit above the measured 319 MiB, but these shapes
come from a search of short patterns, not a proof that nothing costs more.

A mailbox with more than 16,384 different senders loses the speedup on a full pass, and costs what
it did before these caches.

## Running locally

```sh
make check                              # lint and every test, on up to 12 workers
make check PYTEST_ARGS='-m "not slow"'  # what a pull request's jobs run
make check JOBS=2                       # as many workers as a private runner has CPUs
make check JOBS=0                       # one process, for debugging
```

## Publishing to PyPI

`publish.yml` publishes a release to PyPI with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts this workflow, in the
public repo and the `pypi` environment, through a short-lived OIDC token from GitHub, so no PyPI
token is stored anywhere.

- **When:** a pushed `v*` tag, or a published GitHub release on a `v*` tag; a release on any other
  tag skips. A release made with a new tag can start both; the second run waits for the first,
  finds the files on PyPI and skips them. So a green run for a version already on PyPI uploaded
  nothing; its log says so.
- **Where:** each job runs only if `github.repository == 'legalaspro/proton-mail-mcp'`, so a tag
  or a release in this repo or in a fork starts nothing. It leaves out the macOS gate's
  `visibility` check, which is about billing; PyPI trusts only that repository anyway.
  `tests/test_ci_workflows.py` holds the workflow to this, and `tests/test_cli_dispatch.py` keeps
  every other file from uploading. This gate is in the file, though: someone who can push a branch
  could change it there, so the `pypi` environment's tag rule below is what holds publishing to
  tags.
- **Build:** `uv build` makes the sdist and builds the wheel from it. The job stops unless the tag
  is `v` followed by the version that both file names and both versions in `server.json` carry,
  then hands the two files on as an artifact.
- **Publish:** only this job may ask for an OIDC token (`id-token: write`). It checks nothing out
  and runs no code from the repository: `pypa/gh-action-pypi-publish` uploads the files, with
  attestations.
- **Not waited for:** the tag also starts a Full run, and publishing does not wait for it. Tag a
  commit whose Full run passed; the `pypi` environment's required reviewer holds the upload until
  it has.
- **Before the tag:** check that the `Homepage` in `pyproject.toml` returns HTTP 200. PyPI cannot
  change a published version's links, and the MCP Registry entry's `websiteUrl` is the same URL.
- **The plugin's lock:** `plugin/uv.lock` records what the Claude Code plugin's
  `uvx mcp-proton-mail==<version>` resolves to, for the Claude directory's review; uvx does not
  read it. A new version reaches PyPI only with the upload, so lock it against the built files
  first: `uv build` at the root, then `uv lock --find-links ../dist` in `plugin/`, which records
  that one package as coming from `../dist`. After the upload, run
  `uv lock --upgrade-package mcp-proton-mail` in `plugin/` and commit the lock, which then names
  PyPI's files and hashes; a plain `uv lock` keeps `../dist`. Submit the plugin to the directory
  only after that.

### Setting it up, once, by hand

On PyPI, under **Your account**, **Publishing**, add a pending GitHub publisher:

| Field | Value |
| --- | --- |
| PyPI project name | `mcp-proton-mail` |
| Owner | `legalaspro` |
| Repository name | `proton-mail-mcp` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

The project name is not the repository's: PyPI refuses `proton-mail-mcp` as too close to an
existing project's name. The pending publisher became the project's trusted publisher when
`publish.yml` uploaded 1.30.0, which created `mcp-proton-mail` on PyPI.

In the public repo, under **Settings**, **Environments**, create `pypi`, limit its deployments to
tags matching `v*`, and add a required reviewer. PyPI's trust names the repository, the workflow
file and the environment, not the branch or tag, so without the tag rule a run on any branch whose
`publish.yml` was edited could publish. The reviewer approves an upload only after the tag's Full
run has passed: a version on PyPI cannot be replaced. Recommended as well: a tag ruleset that lets
only maintainers create `v*` tags.

### The MCP Registry, by hand

Nothing here lists the server in the [MCP Registry](https://registry.modelcontextprotocol.io/).
The registry checks that the PyPI package exists and that its README carries the `mcp-name` line,
so list it once PyPI shows the version, from a checkout of the tag in the public repo:

```sh
mcp-publisher login github   # signs in as legalaspro, which owns io.github.legalaspro/*
mcp-publisher publish        # reads server.json
```

It stays by hand because `mcp-publisher` is a downloaded program, not an action pinned to a commit,
and has to wait for PyPI.

## Pinned tools

Every action is pinned to a full commit, with its version in a comment, every checkout sets
`persist-credentials: false`, and every workflow starts from `contents: read`, because the public
repo runs these on pull requests from forks. Dependabot proposes new pins weekly in the release
repo. uv is pinned to one version, in `check.yml`, in both places in `installer.yml` and in
`publish.yml`, which Dependabot does not update. Change them together;
`tests/test_ci_workflows.py` holds all four to one version.

`actions/setup-python` no longer sets `check-latest: true`. Jobs use the patch release already on
the runner rather than look for a newer one, which skips a lookup and sometimes a download. A new
Python patch therefore reaches CI when the runner image picks it up.

## Required checks

`main` has no required checks today, and a docs-only pull request starts no run. If `main` ever
requires a check, GitHub leaves a required check waiting forever on a pull request whose path
filter skipped it. So either add a small always-on workflow that reports the same job names on
docs-only changes, or remove the `paths-ignore` from `check.yml`.

Some test files read the docs (such as `test_send_typed_ok.py` and
`test_send_typed_ok_clients.py`, for the wording of the typed-OK warning). A docs-only pull request
skips them, and the push to `main`, which has no path filter on Checks, reports any break.

## Costs

Billed minutes per push, using 1x for Linux and 2x for Windows, with each job rounded up to a whole
minute. GitHub's current price list is $0.006 a minute for Linux and $0.010 for Windows, so 2x
slightly overstates Windows. Each number is either **measured** on GitHub in the private dev repo
or **estimated**. The rows split at native Windows support, which made the Windows job run the
whole suite.

| Push | Linux | Windows (x2) | Installer | Billed | Wait | |
| --- | --- | --- | --- | --- | --- | --- |
| Docs only | - | - | - | 0 | 0 | by design |
| Pull request, before Windows support, first measured run | 7 | 61 s, rounded to 2 min = 4 | - | **11** | **6.3 min** | **measured** |
| Pull request, before Windows support, second measured run | 502 s = 9 | 53 s, rounded to 1 min = 2 | - | **11** | **8.4 min** | **measured** |
| Pull request, before Windows support | 7-10 | 1-2 min = 2-4 | - | 9-14 | 6-10 min | estimated |
| Pull request, after Windows support | 7-10 | 14-17 min = 28-34 | - | 35-44 | 14-17 min | estimated |
| `main`, before Windows support | 11-16 | 1-2 min = 2-4 | 0 or 5 | 13-25 | 11-16 min | estimated |
| `main`, after Windows support | 11-16 | 14-17 min = 28-34 | 0 or 5 | 39-55 | 14-17 min | estimated |

**The 50-minute target holds for a pull request's push, not for every push.** A push to `main` after
Windows support can reach about 55 (estimated) when it also starts the installer workflow, which
billed 5 (**measured**: the wheel smoke 2, and each of the three containers 1).

Linux `not slow` runs from the first measured run above (7) to the measured 9:45 at an earlier
commit on the same branch (10). The `main` rows scale the measured 1020 s for all tests by the same
range, and all are estimated. The Windows figures after Windows support are scaled from the
1744 s measured on the Windows support branch, as that job has never run here. GitHub's runners
can take about twice as long on the same tests (estimated, from the spread between runs), which
would double a row: a Windows job that slow reaches its 25-minute timeout, so the run fails after
billing about 57 (estimated).

A Full run by hand bills about 45 before Windows support and 120-160 after it (estimated). That is
why it is by hand. Checks by hand on one Python costs about the same as a pull request's push, or 17
with `full` before Windows support (**measured** on Python 3.14).

The longest job sets the wait. Before this file existed a push billed 232 minutes and took 30.6
(measured, on the Windows support branch). Each `make check` lists its 15 slowest tests. If a job
grows, look there first, and fix or mark what shows up.
