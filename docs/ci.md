# CI

What GitHub Actions runs, where and at what cost, and how a release reaches PyPI. The same
workflow files run in a private dev repo, where every minute is billed, and in the public release
repo, where hosted runners are free. The dev repo runs nothing on its own, and `make check` is its
gate. The release repo runs everything: on each sync pull request, every night and on each tag.
Only the release repo publishes.

## Two repos, one set of workflows

| | Private dev repo | Public `legalaspro/proton-mail-mcp` |
| --- | --- | --- |
| Minutes | billed | free on hosted runners |
| Gate before a merge | [`make check`, run locally](#the-local-gate) | the [sync pull request's](#how-a-change-reaches-the-release-repo) 12 jobs |
| Pull request | nothing | Checks: Linux and Windows, Python 3.12, `not slow`. Full run: Linux and macOS, Python 3.12 and 3.14, all tests. Installer: all six jobs |
| Docs-only pull request | nothing | nothing |
| Push to `main` | nothing | Checks: Linux with the slow tests, Windows `not slow`; Installer if its paths changed |
| Every night, 03:17 UTC | nothing | Full run: Linux, Windows and macOS, Python 3.12, 3.13 and 3.14, all tests, with Installer |
| `v*` tag | nothing | the same Full run, and [Publish](#publishing-to-pypi) |
| Published GitHub release | nothing | [Publish](#publishing-to-pypi) |
| By hand | Checks, Installer or a Full run, on Linux and Windows | the same, plus macOS (in Checks only with `full`), and Python 3.13 in a Full run |
| macOS | **never** | in every Full run and every Installer run |
| Dependabot | **never** | weekly pull requests for the Python dependencies and the actions |

**No macOS job can run in the private dev repo**, and nothing runs there that nobody started by
hand. Each macOS runner, and each job a pull request, a push, a tag or the nightly would start,
sits behind this condition:

```yaml
github.repository == 'legalaspro/proton-mail-mcp' && (github.event.repository.visibility == 'public' || vars.FREE_MINUTES == 'true')
```

The name check stands outside the parentheses, so the condition is false in the dev repo and in
forks, whatever variables they set. In the release repo it needs the event to say that the repo is
public, or the repo's [`FREE_MINUTES` variable](#the-free_minutes-variable) to be `true`. Where it
is false, test macOS by hand (see [install.md](install.md)). The public Full run of 2 Nov 2026 ran
macOS on Python 3.12, 3.13 and 3.14: two tests failed there, both fixed since in the tests
themselves, and the fixes have not run on macOS yet.

Don't write this as `github.event.repository.private == false`: GitHub converts a missing value to
0, and false to 0, so that is true on any event with no `repository`. A string comparison fails
closed. `tests/test_ci_workflows.py` holds every workflow to this.

In the dev repo the same triggers still start runs, and all their jobs skip. A run whose jobs are
all skipped bills nothing. With the workflows disabled in the repo's Actions settings, nothing
starts at all.

### The FREE_MINUTES variable

GitHub's documents don't say that a `schedule` event carries `github.event.repository`, and other
projects have found it missing there. Without it the visibility check is false, so the nightly
would skip every job and still show green. Python 3.13, Windows 3.14 and the Windows slow tests
would then run in the release repo only on tags.

So the release repo also has a repository variable, `FREE_MINUTES`, set to `true` under
**Settings**, **Secrets and variables**, **Actions**, **Variables**. A maintainer sets it there by
hand, once; no workflow creates it.

- Every gate in every `if:` and matrix carries it, so it brings back the whole nightly: macOS,
  Python 3.13 and the installer workflow, not just some of the jobs. `tests/test_ci_workflows.py`
  checks that each gate does, and that nothing else in the workflows reads a variable.
- It does nothing in the dev repo or a fork, because of the name check. Don't set it there.
- Delete it before the release repo ever goes private: it would let every run there bill, macOS
  at 10x.

The visibility check first ran in the public repo on 2 Nov 2026, in a Full run started by hand, and
the macOS jobs ran. Until the variable is set, check after the first nightly that its jobs ran
rather than showing as skipped.

**Dependabot runs only in the release repo**, where its pull requests get CI for free. Here they
would get none, or billed runs if this repo's pull requests ever run CI again, so this repo keeps
its config at `release/dependabot.yml`, where Dependabot does not look. The release sync moves that
file to `.github/dependabot.yml` in the release tree, unchanged, and leaves no
`release/dependabot.yml` there. `tests/test_ci_workflows.py` fails on a tree that has both files
or neither. Each sync replaces the release tree, so make an update that Dependabot proposes in this
repo: one merged only in the release repo is lost at the next sync.

## What runs when in this repo

| Event | What runs |
| --- | --- |
| Pull request, push to `main`, `v*` tag, the nightly | nothing: every job skips |
| **Checks** by hand | Linux and Windows on the Python you name, `not slow`, or all tests with `full`; the dry runs |
| **Installer** by hand | the wheel smoke on Linux, and the Fedora, AlmaLinux and Arch dry runs |
| **Full run** by hand | Linux and Windows, Python 3.12 and 3.14, all tests and the dry runs; then Installer |

A run by hand bills (see [Costs](#costs)). Start one when a change needs what `make check` can't
give you here, such as Windows, another Python or the installer containers. Everything else runs
for free on the sync pull request.

### The local gate

This repo runs no CI on a pull request, so `make check` is the gate. It runs the lint and the
tests that a Checks job runs; [Running locally](#running-locally) has the variants.

- Before each push, run at least `make check PYTEST_ARGS='-m "not slow"'`, the selection a Checks
  job runs on a pull request.
- Before a pull request merges, run `make check`, with every test, from a clean checkout of the
  branch, and put its summary line (counts, time and commit) in the pull request.
- A pre-push hook in each clone, `.git/hooks/pre-push`, can do the first:

  ```sh
  #!/bin/sh
  exec make check PYTEST_ARGS='-m "not slow"'
  ```

It can't cover Windows, macOS, a Python you don't have, the installer containers or a clean runner.
Those first run on the sync pull request, so a break there needs a fix-forward pull request here
and a new sync.

## How a change reaches the release repo

The release sync opens a pull request in the release repo, from a sync branch to `main`, whose
ruleset requires one. Before it pushes the sync branch, it runs
`python scripts/check_plugin_lock.py` on the release tree and stops if that fails: the Claude
directory refuses a plugin lock that names anything but PyPI (see
[the plugin's lock](#publishing-to-pypi)). Each Checks job runs it again before the tests, so such
a lock also fails a required check by name.

A `pull_request` run uses the workflow files of the pull request's merge ref, so a sync that
changes these files runs the new ones. A sync that is not docs-only runs 12 jobs:

- **Checks**: Linux and Windows, Python 3.12, `not slow`. These are the
  [required checks](#required-checks). pytest stops after 5 failures, and the first failing job
  cancels the other.
- **Full run**: Linux and macOS, Python 3.12 and 3.14, all tests. It reports every runner and every
  failure, so that one fix-forward pull request can fix them all.
- **Installer**: the wheel smoke on Linux and macOS, and the Fedora, AlmaLinux, Arch and macOS dry
  runs.

Merge when all 12 pass. A new push to the pull request cancels its older runs in all three
workflows. Pushes to `main`, tags, the nightly and runs by hand never cancel.

After the merge, the push to `main` runs Checks again, with the slow tests on Linux, and Installer
if its paths changed. It no longer starts a Full run. The nightly runs that on `main`, with what no
pull request runs: Python 3.13, Windows 3.14 and the Windows slow tests. A failure there shows only
after the merge, so check the nightly after each sync. GitHub turns off a public repo's schedule
after 60 days without activity.

A `v*` tag runs the same Full run, and Publish, which does not wait for it: tag a commit whose
nightly or Full run passed (see [Publishing to PyPI](#publishing-to-pypi)).

Each Checks job also runs the installer's quick dry runs: `shellcheck` and `install.sh` under the
service preflight on Linux, and `install.ps1` on Windows. They take seconds. The installer workflow
holds what takes longer: the wheel smoke and the three Linux containers, plus macOS in the release
repo. There it runs on each pull request to `main` that is not docs-only, on a push to `main` that
changes its paths, and in every Full run. Here it runs only by hand: if you change the installer,
run `make wheel-smoke`, or run **Installer** by hand on the branch.

## Where each test and platform runs

| Coverage | Here | Release repo pull request | Release repo, after the merge |
| --- | --- | --- | --- |
| Your OS and Python, every test | `make check` | | |
| Linux 3.12, `not slow` | **Checks** by hand | Checks | |
| Linux 3.12, every test | **Full run** by hand | Full run | push to `main`, nightly, tag |
| Linux 3.14, every test | **Full run** by hand | Full run | nightly, tag |
| Windows 3.12, `not slow` | **Checks** by hand | Checks | push to `main` |
| Windows, every test; Windows 3.14 | **Full run** by hand | no | nightly, tag |
| macOS 3.12 and 3.14, every test | never | Full run | nightly, tag |
| Python 3.13, every OS | **Checks** by hand, on Linux and Windows | no | nightly, tag |
| Wheel smoke on Linux; Fedora, AlmaLinux and Arch dry runs | `make wheel-smoke`, or **Installer** by hand | Installer | push to `main` if its paths changed, nightly, tag |
| Wheel smoke and dry runs on macOS, bash 3.2 | never | Installer | the same |

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
make check PYTEST_ARGS='-m "not slow"'  # what a Checks job runs on a pull request
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
  read it. The directory reports a lock that names any source but PyPI, such as the `../dist`
  that `uv lock --find-links ../dist` records, as "Launcher lock invalid". So the lock names only
  PyPI's files and hashes, and `scripts/check_plugin_lock.py` refuses anything else: in
  `make check`, in each Checks job before the tests, and in the
  [release sync](#how-a-change-reaches-the-release-repo) before it pushes.
  A new version reaches PyPI only with the upload, so the release leaves the plugin on the
  version before it; `tests/test_packaging.py` lets the plugin trail by one release, no more.
  After the upload, set the new version in `plugin/.claude-plugin/plugin.json`,
  `plugin/.mcp.json`, `plugin/pyproject.toml` and `plugin/README.md`, run
  `uv lock --upgrade-package mcp-proton-mail` in `plugin/`, and sync that change. Submit the
  plugin to the directory only after that.

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

In the release repo, a ruleset on `main` requires a pull request and the two Checks jobs,
`ubuntu-latest, Python 3.12` and `windows-latest, Python 3.12`. The Full run and Installer jobs
report on every sync pull request but are not required. To require them, add these to the
ruleset:

- Full run: `checks (3.12) / ubuntu-latest, Python 3.12`,
  `checks (3.12) / macos-latest, Python 3.12`, `checks (3.14) / ubuntu-latest, Python 3.14` and
  `checks (3.14) / macos-latest, Python 3.14`;
- Installer: `Wheel smoke (ubuntu-latest)`, `Wheel smoke (macos-latest)`, `fedora:latest (dry run)`,
  `almalinux:9 (dry run)`, `Arch Linux (dry run)` and `macOS (dry run)`.

Check first that the wheel smoke passes on `main`, or every sync pull request waits on it. A
docs-only pull request starts none of the three workflows, and GitHub leaves a required check
waiting forever on a pull request whose path filter skipped it. So merge a docs-only sync with the
ruleset's bypass, or add a small always-on workflow that reports the same job names on docs-only
changes. This repo's `main` requires no checks, since none run.

Some test files read the docs (such as `test_send_typed_ok.py` and
`test_send_typed_ok_clients.py`, for the wording of the typed-OK warning). A docs-only pull request
skips them in the release repo, and the push to `main`, which has no path filter on Checks, reports
any break. Here, run them and `tests/test_docs.py` before a docs-only pull request.

## Costs

This repo bills only what is started by hand: every other job skips, and a skipped job bills
nothing. Billing counts 1x for Linux, 2x for Windows and 10x for macOS, with each job rounded up to
a whole minute. GitHub's current price list is $0.006 a minute for Linux and $0.010 for Windows, so
2x slightly overstates Windows. Each number is either **measured** on GitHub or **estimated**.

| Run here | Jobs | Billed | Wait | |
| --- | --- | --- | --- | --- |
| Pull request, push to `main`, tag, nightly | none run | 0 | 0 | by design |
| **Checks** by hand, one Python, `not slow` | 2 | 38 (27-47) | about 15 min | **measured**, as pull request runs, 1 Nov 2026 |
| **Installer** by hand | 4 | 6 (5-7) | about 2 min | **measured**, as push runs, 1 Nov 2026 |
| **Full run** by hand | 4, then Installer's 4 | 105-175 | about 35 min | estimated |

Windows is about 80% of a Checks run's bill and sets its wait. GitHub's runners can take about
twice as long on the same tests (estimated, from the spread between runs): a Windows job that slow
reaches its 25-minute timeout, and the run fails after billing about 57 (estimated). The Full run's
range comes from Linux all tests on 3.12 and 3.14 (1,017 and 1,022 s, **measured** on 1 Nov 2026)
and an every-test Windows run of about 33 minutes on a temporary branch.

The release repo is public, so its runs are free. Its job minutes show the size of each run, and
what it would bill if it were private, which is why each of these jobs sits behind the gate:

| Run in the release repo | Jobs | Job minutes | If private | |
| --- | --- | --- | --- | --- |
| Pull request | 12 | about 52, wait 13-17 min | about 209 | estimated from each job's median, 2 Nov 2026 |
| Push to `main` | Checks 2, and Installer 6 if its paths changed | 20-21, and 8 | 33-45, and 26 | **measured**, 2 Nov 2026 |
| Nightly, or a tag's Full run | 15 | 86-98, wait 13-14 min | 289-348 | **measured** on the Full runs of 2 Nov 2026 |

The nightly comes to about 2,600-2,900 job minutes a month (estimated). The longest job sets the
wait. Each `make check` lists its 15 slowest tests. If a job grows, look there first, and fix or
mark what shows up.
