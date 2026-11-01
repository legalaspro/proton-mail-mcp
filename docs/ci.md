# CI

What GitHub Actions runs, where, and what it costs. This repo is private, so every minute is
billed, and the same workflow files stay cheap here and full in the public release repo.

## Two repos, one set of workflows

| | Private `legalaspro/proton-mail-mcp-dev` | Public `legalaspro/proton-mail-mcp` |
| --- | --- | --- |
| Minutes | billed | free on hosted runners |
| Pull request | one Linux and one Windows job, Python 3.12, `not slow` | the same |
| Push to `main` | Linux with slow tests, Windows `not slow`; the installer workflow if its paths changed | the same, and a Full run |
| `v*` tag | nothing | Full run with the installer workflow |
| Every Monday | nothing | Full run with the installer workflow |
| Full run | by hand only | also by hand |
| Full run covers | Linux and Windows, Python 3.12 and 3.14 | Linux, Windows and macOS, Python 3.12, 3.13 and 3.14 |
| macOS | **never** | in every Full run and the installer workflow |

**No macOS job can run in the private repo.** Each macOS runner sits behind this condition:

```yaml
github.repository == 'legalaspro/proton-mail-mcp' && github.event.repository.visibility == 'public'
```

It needs both parts, so it stays false here, in forks, and in the release repo for as long as that
repo is private. **So nothing checks macOS on GitHub until the release repo goes public.** Before
then, test macOS by hand (see [install.md](install.md)). The last macOS run passed on 1 Nov 2026 on
Python 3.12 and 3.14 (run 41300324368).

**The condition only protects a run that uses these files.** The release repo is private today and
still has its own older workflows, whose `installer.yml` runs macOS jobs with no condition on every
push and pull request. macOS bills at 10x in a private repo. So until that repo has these files or
goes public, any push there that doesn't carry them pays that rate. A push that carries them uses
them, so it is safe.

Don't write this as `github.event.repository.private == false`: GitHub converts a missing value to
0, and false to 0, so that is true on any event with no `repository`. A string comparison fails
closed. `tests/test_ci_workflows.py` holds every workflow to this.

Nobody has run this condition in a public repo yet. On the first Monday, tag or `main` push there,
check that the macOS jobs ran rather than showing as skipped. GitHub's documents don't confirm that
`visibility` is present on a `schedule` event; if it isn't, the Monday run skips without warning,
though it can't overspend. The release repo also needs these workflow files: this repo's pull
requests do not put them there.

A run whose jobs are all skipped bills nothing, so the Full run entries that appear here on each
push to `main` and every Monday cost nothing.

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

## Pinned tools

`astral-sh/setup-uv` is pinned to a commit and uv to one version, in `check.yml` and in both places
in `installer.yml`, because the public repo runs these on pull requests from forks. Change them
together; `tests/test_ci_workflows.py` holds all three to one version.

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
slightly overstates Windows. Each number is **measured** on GitHub or **estimated**.

| Push | Linux | Windows (x2) | Installer | Billed | Wait | |
| --- | --- | --- | --- | --- | --- | --- |
| Docs only | - | - | - | 0 | 0 | by design |
| Pull request, before #93, run 41303018384 at cf31d00 | 7 | 61 s, rounded to 2 min = 4 | - | **11** | **6.3 min** | **measured** |
| Pull request, before #93, run 41306449186 at ec9a9cf | 502 s = 9 | 53 s, rounded to 1 min = 2 | - | **11** | **8.4 min** | **measured** |
| Pull request, before #93 | 7-10 | 1-2 min = 2-4 | - | 9-14 | 6-10 min | estimated |
| Pull request, after #93 | 7-10 | 14-17 min = 28-34 | - | 35-44 | 14-17 min | estimated |
| `main`, before #93 | 11-16 | 1-2 min = 2-4 | 0 or 5 | 13-25 | 11-16 min | estimated |
| `main`, after #93 | 11-16 | 14-17 min = 28-34 | 0 or 5 | 39-55 | 14-17 min | estimated |

**The 50-minute target holds for a pull request's push, not for every push.** A push to `main` after
#93 can reach about 55 (estimated) when it also starts the installer workflow, which billed 5
(**measured**, run 41306446898: the wheel smoke 2, and each of the three containers 1).

Linux `not slow` runs from the first measured run above (7) to the measured 9:45 at 19d9bc7, an
earlier commit on this branch (10). The `main` rows scale the measured 1020 s for all tests by the same
range, and all are estimated. Windows after #93 is scaled from #93's measured 1744 s and has never
run. GitHub's runners can take about twice as long on the same tests (estimated, from the spread
between runs), which would double a row: a Windows job that slow reaches its 25-minute timeout, so
the run fails after billing about 57 (estimated).

A Full run by hand bills about 45 before #93 and 120-160 after it (estimated). That is why it is by
hand. Checks by hand on one Python costs about the same as a pull request's push, or 17 with `full`
before #93 (**measured** on Python 3.14, run 41306444809).

The longest job sets the wait. Before this file existed a push billed 232 minutes and took 30.6
(measured, #93 at 77fbde1). Each `make check` lists its 15 slowest tests. If a job grows, look
there first, and fix or mark what shows up.
