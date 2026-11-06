# CI

What GitHub Actions runs, where and at what cost, and how a release reaches PyPI, GitHub and the
MCP Registry. The same workflow files run in a private dev repo, where every minute is billed, and
in the public release repo, where hosted runners are free. The dev repo runs nothing on its own,
and `make check` is its gate. The release repo runs everything: three quick jobs on each sync pull
request, and the full matrix every night and on each tag. Only the release repo publishes.

## Two repos, one set of workflows

| | Private dev repo | Public `legalaspro/proton-mail-mcp` |
| --- | --- | --- |
| Minutes | billed | free on hosted runners |
| Gate before a merge | [`make check`, run locally](#the-local-gate) | the [sync pull request's](#how-a-change-reaches-the-release-repo) 3 Checks jobs |
| Pull request | nothing | Checks: Linux on Python 3.12 and 3.14, and macOS on 3.12 with the wheel smoke, `not slow`. Installer, all six jobs, only if its paths changed |
| Docs-only pull request | nothing | none of these workflows |
| Push to `main` | nothing | the same three Checks jobs |
| Every night, 03:17 UTC | nothing | Full run: Linux, Windows and macOS, Python 3.12, 3.13 and 3.14, all tests, with Installer |
| `v*` tag | nothing | the same Full run, and [Publish](#releasing): PyPI, then the GitHub release and the MCP Registry |
| Published GitHub release | nothing | [Publish](#releasing) |
| By hand | Checks, Installer or a Full run, on Linux and Windows | the same, plus macOS (in Checks only with `full`), and Python 3.13 in a Full run |
| Windows | by hand only | every night, on each tag and by hand; no pull request |
| macOS | **never** | the one macOS job of each pull request and push to `main`, every Full run and every Installer run |
| CodeQL | **never** | GitHub's default setup, not a file here: each pull request, each push to `main` and weekly |
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
would skip every job and still show green. Windows, Python 3.13 and the slow tests would then run
in the release repo only on tags.

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
for free in the release repo: three jobs on the sync pull request, and the rest every night.

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
macOS and Python 3.14 first run on the sync pull request, and the installer containers there too
when the installer's paths changed. Windows, Python 3.13 and the slow tests on any other OS first
run in the release repo's nightly, after the merge. A break in either needs a fix-forward pull
request here and a new sync, so run **Checks** or a **Full run** by hand first when a change is
likely to break Windows.

## How a change reaches the release repo

The release sync opens a pull request in the release repo, from a sync branch to `main`, whose
ruleset requires one. Before it pushes the sync branch, it runs
`python scripts/check_plugin_lock.py` on the release tree and stops if that fails: the Claude
directory refuses a plugin lock that names anything but PyPI (see
[the plugin's lock](#the-plugin-after-the-upload)). Each Checks job runs it again before the
tests, so such a lock also fails a required check by name, and then
`python scripts/check_versions.py`, which fails one whose files name different releases
([Releasing](#releasing)).

A `pull_request` run uses the workflow files of the pull request's merge ref, so a sync that
changes these files runs the new ones. A sync that is not docs-only runs three **Checks** jobs,
the [required checks](#required-checks), all with `-m "not slow"`:

- `ubuntu-latest, Python 3.12`: the plugin's lock and the versions, `ruff check .` and
  `ruff format --check .` on the whole tree, `make check`, and the installer's quick dry runs.
- `ubuntu-latest, Python 3.14`: the same, on the newest Python.
- `macos-latest, Python 3.12`: the plugin's lock, the versions and `make check`, then the wheel
  smoke's macOS part: the wheel built, its files, metadata and entry points, its dry runs through
  `uvx`, and its server starting and refusing a request without a token. The applied quick start
  (`setup` against stand-ins, `connect`, the token, pipx and the installs by name) needs Linux, so
  only Installer's Linux wheel job runs it.

pytest stops after 5 failures in each job, and no job cancels another, so that one fix-forward pull
request can fix every runner's failures. GitHub's CodeQL default setup also analyzes each pull
request. A sync that changes the installer's paths, the list in `installer.yml` (the install
scripts, the installer, the commands, settings, stores and file calls it drives, their tests,
`pyproject.toml` and `installer.yml` itself), also runs **Installer**: the wheel smoke on Linux
and macOS, and the Fedora, AlmaLinux, Arch and macOS dry runs. No pull request starts a Full run.

Merge when they pass. A new push to the pull request cancels its older runs in Checks and
Installer. Pushes to `main`, tags, the nightly and runs by hand never cancel: outside a pull
request, each run of Checks, the Full run and Installer has a concurrency group of its own, and
Publish never cancels.

After the merge, the push to `main` runs the same three Checks jobs again. The nightly runs the
Full run on `main`, with what no pull request runs: Windows, Python 3.13, the slow tests on every
OS, and Installer. A failure there shows only after the merge, so check the nightly after each
sync. GitHub turns off a public repo's schedule after 60 days without activity.

A `v*` tag runs the same Full run, and Publish, which does not wait for it: tag a commit whose
nightly or Full run passed (see [Releasing](#releasing)).

Each Checks job also runs the installer's quick dry runs: `shellcheck` and `install.sh` under the
service preflight on Linux, and `install.ps1` on Windows, which runs in a Full run or by hand. They
take seconds. The installer workflow holds what takes longer: the wheel smoke and the three Linux
containers, plus macOS in the release repo. There it runs on each pull request to `main` that
changes its paths, and in every Full run. Here it runs only by hand: if you change the installer,
run `make wheel-smoke`, or run **Installer** by hand on the branch.

## Where each test and platform runs

| Coverage | Here | Release repo pull request | Release repo, after the merge |
| --- | --- | --- | --- |
| Your OS and Python, every test | `make check` | | |
| Linux 3.12 and 3.14, `not slow`; the whole tree's lint | **Checks** by hand | Checks | push to `main` |
| Linux 3.12 and 3.14, every test | **Full run** by hand | no | nightly, tag |
| macOS 3.12, `not slow`, and the wheel smoke | never | Checks | push to `main` |
| macOS 3.12 and 3.14, every test | never | no | nightly, tag |
| Windows, any Python and test | **Checks** or **Full run** by hand | no | nightly, tag |
| Python 3.13, every OS | **Checks** by hand, on Linux and Windows | no | nightly, tag |
| Wheel smoke on Linux; Fedora, AlmaLinux and Arch dry runs | `make wheel-smoke`, or **Installer** by hand | Installer, if its paths changed | nightly, tag |
| Dry runs on macOS, bash 3.2 | never | Installer, if its paths changed | nightly, tag |
| CodeQL | never | every pull request | push to `main`, weekly |

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

## Releasing

A release goes from a version bump here to PyPI, a GitHub release and the MCP Registry, and the
Claude Code plugin follows it after the upload:

1. **Bump.** On a branch here, `python scripts/bump_version.py X.Y.Z` sets the version in
   `src/proton_mail_mcp/__init__.py`, in both places in `server.json` and in
   `mcpb/manifest.json`'s version and uvx command. In `CHANGELOG.md` it turns `## Unreleased` into
   `## X.Y.Z - <date>`, or adds that heading with no entries when there is no such section: write
   them, since they become the release's notes. `--dry-run` shows the diff and writes nothing.
   Merge it and sync it to the release repo.
2. **Tag.** In the release repo, push the tag `vX.Y.Z` on a commit whose nightly or Full run
   passed. Publish starts; it does not wait for the tag's own Full run.
3. **PyPI.** Publish builds and checks the tag, then the `pypi` environment's required reviewer
   approves the upload ([Publishing to PyPI](#publishing-to-pypi)).
4. **GitHub release** and **MCP Registry**, side by side, once the upload has succeeded
   ([The GitHub release](#the-github-release), [The MCP Registry](#the-mcp-registry)).
5. **Plugin.** Once PyPI has the version, `python scripts/bump_version.py X.Y.Z --plugin` here,
   then a pull request and a sync ([The plugin, after the upload](#the-plugin-after-the-upload)).

`scripts/check_versions.py` holds the files to one release throughout: the package's version,
both in `server.json`, the bundle's version and the release its uvx command runs, and the newest
release heading in `CHANGELOG.md` must all be the same. The plugin's five files must name one
release, that one or the release before it in `CHANGELOG.md`. Each Checks job runs it before the
tests, Publish runs it with `--tag` before the upload, and `tests/test_versions.py` runs it in
`make check`. Every refusal names the file and what to run.

If Publish refuses a tag before the upload, PyPI has nothing of that version, so release the same
version again. Fix the cause here, merge it and sync it, then in the release repo delete the tag,
with `git tag -d vX.Y.Z` and `git push origin :refs/tags/vX.Y.Z`, and push `vX.Y.Z` again on the
fixed commit. Don't bump past it: `CHANGELOG.md` would then hold a release that never reached PyPI,
the plugin would trail the next by two, and `scripts/check_versions.py` refuses that. To drop the
version anyway, turn its `## X.Y.Z - <date>` heading back into `## Unreleased` first, then bump to
the new one. Once PyPI has a version, it is final, and a fix is a new release.

### Publishing to PyPI

`publish.yml` publishes a release to PyPI with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts this workflow, in the
public repo and the `pypi` environment, through a short-lived OIDC token from GitHub, so no PyPI
token is stored anywhere.

- **When:** a pushed `v*` tag, or a published GitHub release on a `v*` tag; a release on any other
  tag skips. A release made with a new tag can start both; the second run waits for the first,
  finds the files on PyPI and skips them, and so do the release and registry jobs. So a green run
  for a version already on PyPI uploaded nothing; its log says so.
- **Where:** each job runs only if `github.repository == 'legalaspro/proton-mail-mcp'`, so a tag
  or a release in this repo or in a fork starts nothing. It leaves out the macOS gate's
  `visibility` check, which is about billing; PyPI trusts only that repository anyway.
  `tests/test_ci_workflows.py` holds the workflow to this, and `tests/test_cli_dispatch.py` keeps
  every other file from uploading. This gate is in the file, though: someone who can push a branch
  could change it there, so the `pypi` environment's tag rule below is what holds publishing to
  tags.
- **Build:** `uv build` makes the sdist and builds the wheel from it. The job stops unless the tag
  is `v` followed by the version that every file names (`scripts/check_versions.py --tag`) and
  that both file names carry, and unless `CHANGELOG.md` has a section for it with entries
  (`scripts/release_notes.py`). So a tag without notes stops before anything is uploaded. It hands
  on the two files as one artifact, and the notes with `server.json` as another.
- **Publish:** it alone asks for an OIDC token for PyPI (`id-token: write`). It checks nothing out
  and runs no code from the repository: `pypa/gh-action-pypi-publish` uploads the files, with
  attestations.
- **Not waited for:** the tag also starts a Full run, and publishing does not wait for it. Tag a
  commit whose Full run passed; the `pypi` environment's required reviewer holds the upload until
  it has. The release and registry jobs need the upload, so they wait for the reviewer too.
- **Before the tag:** check that the `Homepage` in `pyproject.toml` returns HTTP 200. PyPI cannot
  change a published version's links, and the MCP Registry entry's `websiteUrl` is the same URL.

### The GitHub release

Once the upload has succeeded, the `github-release` job makes the tag's release:
`gh release create vX.Y.Z --verify-tag --title X.Y.Z`, with the version's `CHANGELOG.md` section,
without its heading, as the notes. The build took the notes from the tagged tree, before the
upload.

- **Idempotent:** a tag that has a release already, made by hand or by an earlier run, keeps it
  unchanged and the job passes. Any other answer from `gh release view` fails the job rather than
  guess.
- **Token:** the job's own `GITHUB_TOKEN`, with `contents: write` and nothing else. It checks
  nothing out and runs no code from the repository; the notes come from the build's artifact. A
  release that token makes starts no new Publish run, since GitHub starts no workflow for its own
  token's events.
- **By hand:** a release published by hand on a `v*` tag still starts Publish, as before; the job
  then finds it and leaves it as it is.

### The MCP Registry

Once the upload has succeeded, the `registry` job lists the version in the
[MCP Registry](https://registry.modelcontextprotocol.io/) with `mcp-publisher`, from the tag's
`server.json`. The registry checks that PyPI has the package and that its README carries the
`mcp-name` line, so the job first waits until PyPI's JSON API serves the version, for up to five
minutes.

- **No login, no stored token:** `mcp-publisher login github-oidc` uses the job's GitHub OIDC
  token (`id-token: write`), which the registry trades for a short-lived token for
  `io.github.legalaspro/*`, the namespace of the repository's owner. Nobody signs in and no secret
  is stored.
- **What it trusts:** the registry trusts any workflow of a repository that `legalaspro` owns, not
  this file or an environment. What holds this job to an uploaded tag is its `needs: publish`
  here, behind the `pypi` environment, and the repository gate; a version the registry lists must
  be on PyPI anyway.
- **Idempotent:** it asks the registry for
  `/v0/servers/io.github.legalaspro%2Fproton-mail-mcp/versions/X.Y.Z` first, and skips the publish
  when the answer is 200. Any answer but 200 or 404 fails the job rather than guess.
- **Pinned:** `mcp-publisher` is a downloaded program, not an action, so the job pins its version
  and the sha256 of `mcp-publisher_linux_amd64.tar.gz`, as that release's
  `registry_<version>_checksums.txt` gives it, and `sha256sum --check` refuses other bytes before
  the archive is opened. See [Pinned tools](#pinned-tools) to move it.
- **If it fails:** rerun the failed job. By hand, from a checkout of the tag in the public repo,
  `mcp-publisher login github` signs in as `legalaspro` and `mcp-publisher publish` reads
  `server.json`.

### The plugin, after the upload

`plugin/uv.lock` records what the Claude Code plugin's `uvx mcp-proton-mail==<version>` resolves
to, for the Claude directory's review; uvx does not read it. The directory reports a lock that
names any source but PyPI, such as the `../dist` that `uv lock --find-links ../dist` records, as
"Launcher lock invalid". So the lock names only PyPI's files and hashes, and
`scripts/check_plugin_lock.py` refuses anything else: in `make check`, in each Checks job before
the tests, and in the [release sync](#how-a-change-reaches-the-release-repo) before it pushes.

A new version reaches PyPI only with the upload, so the release leaves the plugin on the version
before it; `scripts/check_versions.py` lets the plugin trail by one release, no more. Once PyPI has
the version, run `python scripts/bump_version.py X.Y.Z --plugin` here. It sets the version in
`plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json`, `plugin/pyproject.toml` and
`plugin/README.md`, runs `uv lock --upgrade-package mcp-proton-mail` in `plugin/`, and puts every
file back if a write or uv fails, the new lock fails either check, or Ctrl-C stops it. Open a pull
request with it and sync it. Submit the plugin to the directory only after that.

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

The GitHub release and the MCP Registry need no setup: the release job uses the workflow's own
token, and the registry trusts GitHub's OIDC token for the namespace of the repository's owner.

## Pinned tools

Every action is pinned to a full commit, with its version in a comment, every checkout sets
`persist-credentials: false`, and every workflow starts from `contents: read`, because the public
repo runs these on pull requests from forks. Dependabot proposes new pins weekly in the release
repo. uv is pinned to one version, in `check.yml`, in both places in `installer.yml` and in
`publish.yml`, which Dependabot does not update. Change them together;
`tests/test_ci_workflows.py` holds all four to one version.

`mcp-publisher` is pinned in `publish.yml`'s `registry` job, by `MCP_PUBLISHER_VERSION` and
`MCP_PUBLISHER_SHA256`, and Dependabot does not update it either. To move it, take the new
version from the [registry's releases](https://github.com/modelcontextprotocol/registry/releases),
and the sha256 of `mcp-publisher_linux_amd64.tar.gz` from that release's
`registry_<version>_checksums.txt`; check that GitHub shows the same digest for the asset, and
change both lines together. `tests/test_ci_workflows.py` holds the job to a version and a hash,
checked before the archive is opened.

`actions/setup-python` no longer sets `check-latest: true`. Jobs use the patch release already on
the runner rather than look for a newer one, which skips a lookup and sometimes a download. A new
Python patch therefore reaches CI when the runner image picks it up.

## Required checks

As of 3 Nov 2026, a ruleset on `main` in the release repo requires a pull request and the three
Checks jobs that every pull request runs: `ubuntu-latest, Python 3.12`,
`ubuntu-latest, Python 3.14` and `macos-latest, Python 3.12`. It no longer requires
`windows-latest, Python 3.12`: Windows runs only in the nightly, on tags and by hand, never on a
pull request, and GitHub leaves a required check that never reports waiting forever. When the
Checks jobs of a pull request change, change the ruleset to match.

Don't require the Installer jobs: they run only when their paths change, and GitHub leaves a
required check waiting forever on a pull request whose path filter skipped it. Nor the Full run's:
no pull request runs it. A docs-only pull request starts none of these workflows, so merge a
docs-only sync with the ruleset's bypass, or add a small always-on workflow that reports the same
job names on docs-only changes. This repo's `main` requires no checks, since none run.

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
| Pull request | 3, and Installer's 6 if its paths changed | about 11, wait about 4-5 min; and 8 | about 45, and 26 | estimated from the sync pull request of 3 Nov 2026 |
| Push to `main` | the same 3 | about 11 | about 45 | estimated, the same jobs |
| Nightly, or a tag's Full run | 15 | 86-98, wait 13-14 min | 289-348 | **measured** on the Full runs of 2 Nov 2026 |

The pull request estimate takes each job from the 12 that the sync pull request of 3 Nov 2026 ran
(**measured**): `make check` without the slow tests took 222 s on Linux 3.12, against 492 s with
them, and the wheel smoke took 5 s on macOS. That pull request ran 12 jobs, about 46 job minutes,
and waited 15 minutes, all of it for the Windows job. macOS bills at 10x, so its one job is most of
the 45 a private repo would bill.

The nightly comes to about 2,600-2,900 job minutes a month (estimated). The longest job sets the
wait. Each `make check` lists its 15 slowest tests. If a job grows, look there first, and fix or
mark what shows up.
