# Windows

Native Windows support is new. It has run on GitHub's Windows Server 2025 runners, including a
real install with a real Task Scheduler task and the real Bridge. **It has not been run on Windows 10
or 11, from a normal account, or with a real Proton account**, so expect rough edges and read
[what was not checked](#what-was-not-checked). The checks that keep your files private are newer
still, and [have been tested far less](#private-files). If you need this to be dependable today, use
[WSL2](https://learn.microsoft.com/windows/wsl/install) and the Linux quick start.

Python 3.12.4 or newer is recommended on Windows: from that version a folder created with
mode `0700` gets access rules for only you, SYSTEM and Administrators. Older 3.12 releases
leave new folders with the rules of the folder above, so a shared folder above refuses.

## How each area behaves

| Area | On Linux and macOS | On Windows |
| --- | --- | --- |
| Locks | `fcntl.flock`, unchanged | `LockFileEx`, shared and exclusive, without waiting. A held lock refuses as busy, as on POSIX, and ends when the file closes. It covers one byte past any data, so it never blocks a read or a write. |
| Where state lives | `XDG_STATE_HOME` or `~/.local/state`, unchanged | `%LOCALAPPDATA%\proton-mail-mcp\state`. XDG variables are ignored. `%APPDATA%` is not used: it can roam to other machines, and this data stays on this one. |
| Who can read it | Owner and mode checks (`0600`, `0700`), unchanged | Windows `stat` has no owner or mode bits, so the server reads each file's and folder's real owner and access rules (`GetSecurityInfo`, by open handle) before it trusts or writes it. Only you, SYSTEM and Administrators may have access; anyone else, a null DACL or rules that cannot be read refuses, and nothing repairs them for you. See [private files](#private-files). **These checks have only been tested as described there.** The action log, saved rules, indexes, attachments folder and env file must also be inside your profile (`%USERPROFILE%` or `%LOCALAPPDATA%`), links resolved, and those folders' own rules are checked too, so an environment variable alone proves nothing. SQLite keeps its spill files in memory there, not in `%TEMP%`. |
| Links | `O_NOFOLLOW` and walks through `dir_fd`, unchanged | Every reparse point at a checked name is refused: symbolic links, junctions and cloud placeholders, so a OneDrive-managed attachments folder can be refused. A directory held open cannot be renamed or removed until it closes, which stands in for `dir_fd`: a name opened in it stays in it. |
| Hard links | A file with a second name is refused, unchanged | The same. |
| File names | Unchanged | A name you give that Windows would read as a device, stream, drive or short alias (`NUL`, `a:b`, `C:x`, `ABCDEF~1`), or that ends in a dot or a space, is refused. A sender's name is changed to a plain one when saved (`CON` becomes `_CON`), and only then: a draft keeps the name as the sender wrote it. |
| Rename durability | The directory is fsynced after a rename, unchanged | A directory cannot be fsynced on Windows, so a power cut just after a rename can undo it. NTFS keeps the volume consistent, and the action log, the record that recovery reads, is flushed on every write as on POSIX. |
| Busy files | A rename or removal does not care who has the file open | Another program holding a file (a virus scanner, say) refuses its rename or removal, and a rename over any open file fails. The server tries for one second, then fails as it does for any file error. A file held against opening counts as busy, and a check of its rules that meets one says so and asks you to try again. |
| Credentials | Keychain, libsecret or pass, unchanged | Windows Credential Manager, which is the default there. The settings' default stays `keychain` elsewhere. |
| PDF reading | Separate process with `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, unchanged | Separate process in a job object with comparable limits (1 GiB of committed memory, 30 s of user-mode CPU) and one process; if the job cannot be set up it reads nothing. The 30-second wall clock stands too. There is no equivalent of `RLIMIT_FSIZE`, so the process could write files if a hostile PDF took it over. Its environment holds only `SYSTEMROOT`. |
| Waking a blocked read | `shutdown` wakes it at once | It does not: the read ends at its socket timeout instead (30 s by default). Deadlines still hold through that timeout, and the request that wanted the read to yield does not wait for it. |
| Services | systemd or launchd, unchanged | Task Scheduler logon tasks that run only while you are logged on, so Credential Manager is available. The service logs to `%LOCALAPPDATA%\proton-mail-mcp\logs\mcp.log`. |
| Bridge | Package or Homebrew, unchanged | `winget install Proton.ProtonMailBridge`, a machine-wide package, so Windows may ask for an administrator's approval; then an Authenticode check that the signature is valid and issued to Proton AG. The task runs `%ProgramFiles%\Proton AG\Proton Mail Bridge\bridge.exe` (or `--bridge-bin`), not the launcher `proton-bridge.exe`, which on Windows starts Bridge and exits, so a task could not see or stop it. The launcher is what applies Bridge's own updates, so update with `winget upgrade`. |
| Installing from Cygwin or MSYS2 | | Refused. They are neither Linux nor Windows programs, and the service needs Python for Windows. |
| Installing from an elevated shell | Root is refused, unchanged | Refused when applying; a dry run is fine. What it created would belong to Administrators, out of reach of a logon task running as you. winget asks for approval itself. |
| Python from the Microsoft Store | | Refused for installing: it keeps `%LOCALAPPDATA%` in a folder of its own, where a logon task cannot find the service. Use python.org or winget. |

## Private files

The server checks who has access. Anyone other than you, SYSTEM and Administrators refuses, at one of
two times.

**At startup, so the server does not start.** These are checked, with each folder from them up to your
profile folder, as far as they exist:

- the action log and saved rules files;
- the identity index folder and the attachments folder, whose rules a saved attachment takes on. A
  file you choose to attach is not checked, as on POSIX;
- the default state folder, when any of these falls back to it;
- the env file, and the service log and its folder.

`setup` checks its folders and the attachments folder too, before it writes anything.

**At each use, so that one store stops being used.** The server keeps running, and a shared file stays
on disk until you fix it. This covers the action log, saved rules and attachments folder again, each
refusing that call, as well as what startup does not reach:

- the search index, with its folder and locks, which is logged as `unsafe_permissions` and turned off,
  so searches read the mailbox instead, as on POSIX;
- the identity index's own files, which go uncertified, so the folder is scanned in full instead;
- the rules state, its lock and their folder, which stop that rules run: it says only that the run
  failed, with no further moves.

The rules file and the protected-senders file you name in settings are not checked, as on POSIX.

A rule giving another account access refuses whether it is explicit, inherited or inherit-only, and
a rule denying access excuses nothing. A file moved in keeps its own rules, so a private profile
says nothing about it. A refusal at startup names the path and who has access, and one at the use of the
action log, saved rules or attachments folder names that store and who has access. The indexes and a
rules run do neither, as above.

**Nothing changes rules for you**: they may be your choice, such as a shared folder. To make a
folder private, in PowerShell, after checking nothing else needs that access:

```powershell
icacls $folder /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
```

Then do the same without `(OI)(CI)` for any file in it that keeps its own rules. This command has
not been run by this project's tests.

One exception: your profile folder itself, and nothing inside it, may also have rules for an app
capability (a SID starting `S-1-15-3-`). That is what a sandboxed app holds in its token, not an
account. A real Windows Server 2025 profile folder has one, which its `AppData` does not inherit,
and refusing it would refuse every install there. The rule's exact rights were not read. A file
directly in your profile folder is refused at use on such a machine, so keep files in a folder below.

**How far this is tested.** On Linux, with a stand-in for the Windows security API that hands back
ACLs byte for byte as Windows lays them out: owner-only, Python's `0700` rules, Everyone and Users,
another user, inherited and inherit-only rules, another owner and a null DACL. That shows what the
checks decide about given rules, not what Windows reports for a real file. Under Wine the real calls
work, but Wine derives rules from Unix modes and keeps none that are set, so it shows little more.
**On real Windows the checks have run only in CI**, on Windows Server 2025, from an elevated runner
account with no second user: `tests/test_localfs.py` sets real rules there, and the suite runs on the
runner's real profile. Its first run is what found the capability rule above, which the stand-in
could not have shown. **They have not run on Windows 10 or 11, from a standard account, or against a
real second account's access**, and the rule for another account in those tests is one a real account
never used. A normal profile may carry other rules this refuses, and the server would then not start:
there is no setting to turn the checks off, so please report it.

## Install

**Not on PyPI yet:** until the first release is published, the by-name commands below do not
install this project, and must not be run: anyone could hold that name on PyPI. Install from a
clone of this repository instead. In PowerShell, read the plan, then apply it:

```powershell
git clone https://github.com/legalaspro/proton-mail-mcp.git; cd proton-mail-mcp
uvx --from . proton-mail-mcp setup --dry-run
uvx --from . proton-mail-mcp setup --client claude-desktop
```

Once it is published, the same two steps by name:

```powershell
uvx proton-mail-mcp setup --dry-run
uvx proton-mail-mcp setup
```

From a checkout, past the default script policy:
`powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -DryRun -BridgeUser you@example.com`.

## What was checked, and how

**Real Windows, on GitHub's `windows-latest` runners (Windows Server 2025).** With the access-rule
checks, the full test suite last ran there on Python 3.12: 8,409 tests passed and 438 were left out.
Most of the 93 added to that count are the stand-in tests of those checks, which real runs replace.
The 3.14 job of that run was cancelled. Pull requests now run only the tests not marked `slow` there,
on 3.12: see
[ci.md](ci.md). Before these checks, the full suite ran on 3.12 and 3.14: 8,394 tests passed and 345
were left out. `tests/windows_skips.py` gives
the reason for 105 of those: POSIX modes, FIFOs, POSIX paths, and two tests that need `shutdown` to
wake a read. The rest carry a mark in their own files: 92 test the stand-ins below, which real runs
replace; 78 are installer flows on a simulated Linux machine that write POSIX paths for real, where
only the plan tests and the real install below cover Windows; 42 are browser tests of the mail
views, which find no Chrome there by the names they look for; and 28 are others, mostly POSIX-only,
such as bash and process groups. A one-off job also ran, on a real runner, from the wheel:

- `install` with the real Task Scheduler: it registered the task, the task started the service, and
  the installer's self-check passed against a small TLS IMAP stand-in for Bridge. Secrets came from
  the real Credential Manager. `create_draft` wrote through the stand-in, and `list_actions` read its
  audit record back. `uninstall` then stopped the task, proved it stopped, deleted it, and kept the data.
- Bridge 3.27.1 from its real installer: `bridge.exe` is where the plan expects it and is signed by
  Proton AG, and the check accepted it and refused Microsoft's `notepad.exe`. Under its task it listened on
  1143 with no account, showed as running, and `schtasks /End` stopped it. The launcher exited in 0.1 s,
  leaving `bridge.exe` running, which is why the task does not use it.
- Real symbolic links and junctions were refused, and an open directory refused a rename and a removal.
- A new `0700` folder had exactly SYSTEM, Administrators and OWNER RIGHTS, and its files inherited them.
- The PDF worker's job limits held: a large allocation failed, a CPU loop was killed, and a child
  process was refused.

**Stand-ins on Linux, in `make check`.** `tests/test_localfs_windows.py`, `tests/test_windows_acl.py`
and `tests/test_windows_compat.py` set `sys.platform` to `win32` and replace `kernel32`, `advapi32`
and `msvcrt`.
Source checks there keep POSIX-only calls inside `localfs.py`.

**Wine 9.0, before CI could run.** The same suite on Windows builds of CPython found one real bug: the
protected-keyword defaults were read as cp1252. Wine has no symbolic links, enforces no job limits
and only stubs `schtasks`, so real runners supersede what it showed.

On Linux, `strace` shows the same file calls before and after this work, in the same order: 1047
calls over the action log, indexes, rules state, saved rules and attachments, including 24 `flock`s,
with the same flags and results. The access-rule checks return before calling anything there.

## What was not checked

- **The access-rule checks anywhere but an elevated Windows Server runner.** See
  [how far this is tested](#private-files).
- **Windows 10 or 11.** Only Windows Server 2025, on a hosted runner.
- **A normal account.** Runners are elevated, so the real install ran with the elevation refusal
  switched off. Its ownership and access rules for a standard user are reasoned, not seen.
- **A logon.** The task was started with `schtasks /Run`; its logon trigger has never fired.
- **A real Proton account or real mail.** Bridge ran with no account, and mail went to a stand-in.
- **`setup`'s prompts and Bridge login**, which need a person at a terminal, and winget itself: the
  job ran Bridge's installer directly, with the hash and switches from winget's manifest.
- **The Microsoft Store Python refusal**, and cloud placeholders such as OneDrive's.
- **Bridge's own updates.** They come through its launcher, which the task does not use, so the plan
  relies on `winget upgrade`.
