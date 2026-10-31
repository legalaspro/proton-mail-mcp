"""Apply or print installer steps; secrets never reach output, argv or errors."""

import csv
import getpass
import hashlib
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from importlib import metadata
from pathlib import Path
from typing import TextIO

from .. import credential_store
from . import selfcheck
from .steps import (
    MARKER,
    PACKAGE,
    BridgeLogin,
    BridgeService,
    CheckDebSignature,
    CheckKey,
    CheckPolicy,
    CheckRpmSignature,
    CheckSameCopy,
    CheckStopped,
    Command,
    DeleteSecret,
    Download,
    EnsureDir,
    Note,
    RemovePath,
    SelfCheck,
    Step,
    StoreSecret,
    WaitForPort,
    WriteFile,
)

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
# Windows file descriptors default to text mode, which would rewrite newline bytes.
_BINARY = getattr(os, "O_BINARY", 0)
MAX_POLICY_BYTES = 64 * 1024
_TIMEOUT = 60
_VERIFY_TIMEOUT = 120
RPM_VERIFIED = "digests signatures OK"
DEBSIG_VERIFIED = "Verified package from"
# The keyring name every policy File must use, and the name the policy is stored under.
DEBSIG_KEYRING = "debsig.gpg"
DEBSIG_POLICY = "bridge.pol"
# gpg --with-colons validity: revoked, expired or invalid keys never count.
_UNUSABLE_VALIDITY = {"r", "e", "i"}
# Where an environment keeps packages. -S: no site, so no .pth file of a copy not yet
# proven runs here; a venv's own sys.prefix needs site, so the venv is named instead.
_SITE_PACKAGES = (
    "import sys, sysconfig; "
    "print(sysconfig.get_path('purelib', 'venv', {'base': sys.argv[1], 'platbase': sys.argv[1]}))"
)
# How long a stopped Bridge may keep its port or process, and how often a just-unloaded
# LaunchAgent may refuse to load again, as launchctl does for a moment after a bootout.
_BRIDGE_STOP_CHECKS = 20
_BRIDGE_START_RETRIES = 3
# In a dist-info directory, what comes from the wheel and decides what runs. The rest
# differs between equal copies: pip and uv each add their own files there.
_COMPARED_METADATA = {"METADATA", "entry_points.txt"}


class InstallError(RuntimeError):
    """A step failed; earlier steps stay applied and a rerun resumes safely."""


class _HTTPSOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise InstallError("Download redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(url: str):
    opener = urllib.request.build_opener(_HTTPSOnlyRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": "proton-mail-mcp-installer"})
    return opener.open(request, timeout=_TIMEOUT)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _running_copy() -> metadata.Distribution:
    return metadata.distribution(PACKAGE)


def _stdin_is_terminal() -> bool:
    # Python sets sys.stdin to None when the process starts with stdin closed.
    return bool(sys.stdin and sys.stdin.isatty())


def on_terminal() -> bool:
    """Whether a person can answer here: this process reads and writes a terminal."""
    return _stdin_is_terminal() and bool(sys.stdout and sys.stdout.isatty())


def terminal_available() -> bool:
    """Whether a hidden prompt can run: getpass reads /dev/tty, or a terminal on stdin."""
    try:
        os.close(os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY))
    except (OSError, AttributeError):
        return _stdin_is_terminal()
    return True


def command_line(argv: Sequence[str], windows: bool = os.name == "nt") -> str:
    if windows:
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _managed(path: Path) -> bool:
    """Installer files carry MARKER; task definitions are UTF-16 with a BOM."""
    try:
        data = path.read_bytes()
        encoding = "utf-16" if data[:2] in {b"\xff\xfe", b"\xfe\xff"} else "utf-8"
        return MARKER in data.decode(encoding)
    except (OSError, UnicodeError):
        return False


class Executor:
    def __init__(
        self,
        *,
        dry_run: bool,
        force: bool = False,
        out: TextIO | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        opener: Callable[[str], object] | None = None,
        prompt: Callable[[str], str] | None = None,
        port_open: Callable[[int], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sudo: Sequence[str] = ("sudo",),
        windows: bool = os.name == "nt",
        assume_yes: bool = False,
        ask: Callable[[str], str] | None = None,
        terminal: Callable[[], bool] = on_terminal,
        check_service: Callable[..., None] = selfcheck.check_service,
        running_copy: Callable[[], metadata.Distribution] = _running_copy,
    ) -> None:
        self.dry_run = dry_run
        self.force = force
        self.out = out or sys.stdout
        self.runner = runner or subprocess.run
        self.opener = opener or _default_opener
        self.prompt = prompt or getpass.getpass
        self.port_open = port_open or _port_open
        self.sleep = sleep
        self.sudo = tuple(sudo)
        self.windows = windows
        self.assume_yes = assume_yes
        self.ask = ask or input
        self.terminal = terminal
        self.check_service = check_service
        self.running_copy = running_copy
        self.changed: set[str] = set()
        self.warnings: list[str] = []

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self.say(f"    warning: {text}")

    def run(self, steps: Sequence[Step]) -> None:
        actions = [step for step in steps if not isinstance(step, Note)]
        number = 0
        for step in steps:
            if isinstance(step, Note):
                self.say(f"  - {step.text}")
                if step.log:
                    self.log(step)
                continue
            number += 1
            prefix = "[dry run] " if self.dry_run else ""
            self.say(f"[{number}/{len(actions)}] {prefix}{step.summary}")
            try:
                getattr(self, f"_{type(step).__name__}")(step)
            except InstallError as error:
                raise InstallError(f"Step {number} ({step.summary}) failed: {error}") from None

    def log(self, step: Note) -> None:
        """Append an applied note to its log file, owner-only; a failure only warns."""
        if self.dry_run:
            self.say(f"    (when applied, this is also written to {step.log})")
            return
        path = Path(step.log)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _BINARY, 0o600)
            with os.fdopen(descriptor, "ab") as handle:
                handle.write(f"{stamp} {step.text}\n".encode())
        except OSError:
            self.warn(f"cannot write {path}; the note above is not logged")
            return
        self.say(f"    logged to {path}")

    # Commands -----------------------------------------------------------------

    def argv(self, step: Command) -> list[str]:
        return [*self.sudo, *step.argv] if step.sudo else list(step.argv)

    def _Command(self, step: Command) -> None:
        argv = self.argv(step)
        if step.when_changed and not self.changed.intersection(step.when_changed):
            self.say(f"    skipped: nothing it depends on changed ({', '.join(step.when_changed)})")
            return
        for name, value in step.env:
            self.say(f"    with {name}={value}")
        self.say(f"    $ {command_line(argv, self.windows)}")
        if self.dry_run:
            self.changed.update(step.marks)
            return
        env = None
        if step.env:
            env = dict(os.environ)
            env.update(step.env)
        capture = step.expect is not None or not step.interactive
        for attempt in range(step.retries + 1):
            try:
                result = self.runner(
                    argv, check=False, env=env, capture_output=capture, text=True, stdin=None
                )
            except OSError:
                raise InstallError(f"cannot run {argv[0]}") from None
            if result.returncode == 0:
                break
            if attempt < step.retries:
                self.sleep(1.0)
        output = ((result.stdout or "") + (result.stderr or "")) if capture else ""
        if result.returncode:
            if step.allow_failure:
                self.say(f"    (ignored exit status {result.returncode})")
                return
            if output.strip():
                self.say(_indent(output))
            raise InstallError(
                f"exit status {result.returncode}" + (f". {step.hint}" if step.hint else "")
            )
        if step.expect is not None:
            if step.expect not in output:
                self.say(_indent(output))
                raise InstallError(f"output does not contain {step.expect!r}")
            self.say(f"    ok: {step.expect}")
        self.changed.update(step.marks)

    def _CheckSameCopy(self, step: CheckSameCopy) -> None:
        """Compare in this process: nothing from the installed copy runs until it passes."""
        self.say("    every file the wheel installed must match this running copy")
        if self.dry_run:
            self.say(f"    (checked when applied; a difference stops the run here. {step.hint})")
            return
        if not self._same_copy(step):
            raise InstallError(
                "pip installed something other than the copy running this installer, or the "
                "two cannot be compared, so this run stops before any service is started or "
                f"restarted. Until a rerun replaces it, {step.venv} holds what pip installed. "
                f"{step.hint}"
            )
        self.say("    ok: the same files")

    def _same_copy(self, step: CheckSameCopy) -> bool:
        argv = [step.python, "-I", "-S", "-c", _SITE_PACKAGES, step.venv]
        try:
            result = self.runner(
                argv, check=False, capture_output=True, text=True, timeout=_TIMEOUT
            )
            site = (result.stdout or "").rstrip("\r\n")
            if result.returncode or not site:
                return False
            found = list(metadata.distributions(name=PACKAGE, path=[site]))
            if len(found) != 1:
                return False
            expected = _copy_files(self.running_copy())
            return bool(expected) and expected == _copy_files(found[0])
        except (OSError, ValueError, ImportError, csv.Error, subprocess.SubprocessError):
            return False

    # Files --------------------------------------------------------------------

    def _EnsureDir(self, step: EnsureDir) -> None:
        path = Path(step.path)
        if self.dry_run:
            state = "exists" if path.is_dir() else f"create (mode {step.mode:04o})"
            self.say(f"    {step.path}: {state}")
            return
        if not path.is_dir():
            try:
                path.mkdir(mode=step.mode, parents=True, exist_ok=True)
            except OSError as error:
                raise InstallError(f"Cannot create {step.path}: {error.strerror}") from None

    def _WriteFile(self, step: WriteFile) -> None:
        path = Path(step.path)
        data = step.content.encode(step.encoding)
        # lstat: a dangling link counts too. Writing would replace the link itself.
        if path.is_symlink():
            remove = command_line(["rm", "--", step.path], self.windows)
            raise InstallError(
                f"{path} is a symbolic link, and the installer never writes or replaces one, "
                f"even with --force. Check where it points, remove the link with `{remove}` "
                "(what it points to stays), then rerun"
            )
        current = None
        if path.exists():
            try:
                current = path.read_bytes()
            except OSError:
                raise InstallError(f"cannot read {path}") from None
        if current is not None and current == data:
            self.say(f"    {path}: unchanged")
            return
        if current is not None and not _managed(path):
            if not self.force:
                raise InstallError(
                    f"{path} exists and was not written by the installer; move it away or "
                    "rerun with --force (the old file is kept as .bak)"
                )
        state = "create" if current is None else "update"
        self.say(f"    {path}: {state} (mode {step.mode:04o})")
        if step.tag:
            self.changed.add(step.tag)
        if self.dry_run:
            return
        if current is not None and not _managed(path):
            # A new name every time: an earlier backup is never replaced.
            backup = _new_file(path.parent, f"{path.name}.", ".bak", current, 0o600)
            self.say(f"    kept the previous file as {backup}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_write(path, data, step.mode)

    def _RemovePath(self, step: RemovePath) -> None:
        """One file the installer wrote, or one empty directory; never anything else.

        A directory goes only with rmdir, so one that holds anything stays; a symbolic
        link is never followed or removed.
        """
        path = Path(step.path)
        if not os.path.lexists(path):
            self.say(f"    {path}: not present")
            return
        if path.is_symlink():
            self.say(f"    {path}: kept (a symbolic link)")
            return
        if step.directory:
            if not path.is_dir():
                self.say(f"    {path}: kept (not a directory)")
                return
            try:
                with os.scandir(path) as entries:
                    empty = next(entries, None) is None
            except OSError:
                empty = False
            if not empty:
                self.say(f"    {path}: kept (not empty)")
                return
        elif not path.is_file():
            self.say(f"    {path}: kept (not a regular file)")
            return
        elif step.managed_only and not _managed(path):
            self.warn(f"{path} was not written by the installer; left in place")
            return
        self.say(f"    {path}: remove")
        if self.dry_run:
            return
        try:
            if step.directory:
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            raise InstallError(f"cannot remove {path}") from None

    def _CheckStopped(self, step: CheckStopped) -> None:
        """The query must prove the service stopped; its output is never shown."""
        self.say(f"    $ {command_line(step.argv, self.windows)}")
        if self.dry_run:
            self.say("    (checked when applied)")
            return
        if not self._proven_stopped(step):
            raise InstallError(f"not proven stopped. {step.hint}")
        self.say("    ok: stopped")

    def _proven_stopped(self, step: CheckStopped) -> bool:
        try:
            result = self.runner(
                list(step.argv), check=False, capture_output=True, text=True, timeout=_TIMEOUT
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        # Only the one answer that means stopped counts; a failed query proves nothing.
        return result.returncode == step.status and (
            not step.states or (result.stdout or "").strip() in step.states
        )

    # Bridge downloads and signatures ------------------------------------------

    def _Download(self, step: Download) -> None:
        self.say(f"    {step.url} -> {step.dest}")
        if self.dry_run:
            return
        if not step.url.startswith("https://"):
            raise InstallError("downloads must use HTTPS")
        # Straight into a new file: one already at dest may not be the installer's, so it
        # is never replaced, and what a failed download wrote stays for you to remove.
        remove = command_line(["rm", "-f", "--", step.dest], self.windows)
        total, created = 0, False
        try:
            with self.opener(step.url) as response:
                final = getattr(response, "geturl", lambda: step.url)()
                if not str(final).startswith("https://"):
                    raise InstallError("download left HTTPS")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY
                descriptor = os.open(step.dest, flags, 0o600)
                created = True
                with os.fdopen(descriptor, "wb") as handle:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise InstallError("download is larger than expected")
                        handle.write(chunk)
        except FileExistsError:
            raise InstallError(
                f"{step.dest} already exists, and the installer never replaces a file it did "
                f"not create in this run. Check it, remove it with `{remove}`, then rerun"
            ) from None
        except (InstallError, OSError, ValueError) as error:
            reason = f"download failed ({type(error).__name__})"
            if isinstance(error, InstallError):
                reason = str(error)
            if created:
                reason += f"; the partial file stays: remove it with `{remove}` before rerunning"
            raise InstallError(reason) from None
        self.say(f"    {total} bytes")

    def _CheckKey(self, step: CheckKey) -> None:
        argv = ["gpg", "--batch", "--with-colons", "--show-keys", step.path]
        self.say(
            f"    $ {command_line(argv, self.windows)}  (expect primary key {step.fingerprint})"
        )
        if self.dry_run:
            return
        try:
            result = self.runner(argv, check=False, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise InstallError("cannot run gpg") from None
        if result.returncode:
            raise InstallError("gpg cannot read the downloaded key")
        check_pinned_key(result.stdout, step.fingerprint)
        self.say(f"    ok: the only primary key is {step.fingerprint}")

    def _CheckRpmSignature(self, step: CheckRpmSignature) -> None:
        self.say(
            f"    temporary rpm database holding only {step.key}; rpm must report {RPM_VERIFIED!r}"
        )
        if self.dry_run:
            for argv in rpm_check_commands("<temporary directory>", step.key, step.package):
                self.say(f"    $ {command_line(argv, self.windows)}")
            return
        # The system rpm database trusts every repository key; this one trusts Proton's only.
        try:
            dbpath = tempfile.mkdtemp(prefix=".rpmdb-", dir=Path(step.package).parent)
        except OSError:
            raise InstallError("cannot create the temporary rpm database") from None
        try:
            init, unkeyed, imported, keyed = rpm_check_commands(dbpath, step.key, step.package)
            self._verify(init, "rpm cannot create the temporary database")
            if RPM_VERIFIED in _output(self._verify(unkeyed)):
                raise InstallError(
                    "rpm verified the package before the pinned key was imported, so the "
                    "temporary database is not isolated; refusing to install"
                )
            self._verify(imported, "rpm cannot import the pinned key")
            checked = self._verify(keyed, "the package is not signed by the pinned key")
            if RPM_VERIFIED not in _output(checked):
                raise InstallError("the package is not signed by the pinned key")
        finally:
            self.say(_kept_scratch("the temporary rpm database", dbpath))
        self.say(f"    ok: {RPM_VERIFIED} with only the pinned key")

    def _CheckDebSignature(self, step: CheckDebSignature) -> None:
        self.say(
            f"    temporary debsig directories holding only the policy and key for "
            f"{step.key_id}; debsig-verify must report {DEBSIG_VERIFIED!r}"
        )
        if self.dry_run:
            commands = debsig_check_commands(
                "<temporary directory>", step.key_id, step.key, step.package
            )
            for argv in commands:
                self.say(f"    $ {command_line(argv, self.windows)}")
            return
        try:
            policy = Path(step.policy).read_bytes()
        except OSError:
            raise InstallError("cannot read the policy") from None
        # The bytes checked here are the bytes debsig-verify reads.
        check_debsig_policy(policy, step.key_id)
        # /etc/debsig and /usr/share/debsig trust every configured origin; these
        # directories trust Proton's policy and pinned key only.
        try:
            root = tempfile.mkdtemp(prefix=".debsig-", dir=Path(step.package).parent)
        except OSError:
            raise InstallError("cannot create the temporary debsig directories") from None
        try:
            unkeyed, dearmor, shown, keyed = debsig_check_commands(
                root, step.key_id, step.key, step.package
            )
            policies, keyrings = Path(root, "policies"), Path(root, "keyrings")
            _make_dirs(policies, keyrings)
            result = self._verify(unkeyed)
            if not result.returncode or DEBSIG_VERIFIED in _output(result):
                raise InstallError(
                    "debsig-verify verified the package before the pinned policy and key "
                    "were in place, so the temporary directories are not isolated; "
                    "refusing to install"
                )
            _make_dirs(policies / step.key_id, keyrings / step.key_id)
            _atomic_write(policies / step.key_id / DEBSIG_POLICY, policy, 0o600)
            self._verify(dearmor, "gpg cannot convert the pinned key")
            # The keyring debsig-verify reads must itself hold only the pinned key.
            listed = self._verify(shown, "gpg cannot read the converted key")
            check_pinned_key(listed.stdout or "", step.fingerprint)
            checked = self._verify(keyed, "the package is not signed by the pinned key")
            if DEBSIG_VERIFIED not in _output(checked):
                raise InstallError("the package is not signed by the pinned key")
        finally:
            self.say(_kept_scratch("the temporary debsig directories", root))
        self.say(f"    ok: {DEBSIG_VERIFIED} with only the pinned policy and key")

    def _verify(self, argv: list[str], failure: str | None = None) -> subprocess.CompletedProcess:
        """Run one verification command; a failure message makes a nonzero exit fatal."""
        self.say(f"    $ {command_line(argv, self.windows)}")
        try:
            result = self.runner(
                argv, check=False, capture_output=True, text=True, timeout=_VERIFY_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            raise InstallError(f"cannot run {argv[0]}") from None
        if failure is not None and result.returncode:
            output = _output(result)
            if output.strip():
                self.say(_indent(output))
            raise InstallError(failure)
        return result

    def _CheckPolicy(self, step: CheckPolicy) -> None:
        self.say(f"    {step.path}: every id must be {step.key_id}, every File debsig.gpg")
        if self.dry_run:
            return
        try:
            data = Path(step.path).read_bytes()
        except OSError:
            raise InstallError("cannot read the policy") from None
        check_debsig_policy(data, step.key_id)
        self.say("    ok: policy names only the pinned key")

    # Credentials --------------------------------------------------------------

    def _StoreSecret(self, step: StoreSecret) -> None:
        where = f"{step.store} ({step.service}, account {step.account}){_at(step.locations)}"
        self.say(f"    save in {where}; the value is never printed")
        if self.dry_run:
            self.changed.add(step.tag)
            return
        if step.source == "generate":
            secret = secrets.token_urlsafe(32)
        elif step.store == "keychain":
            # `security` prompts on the terminal itself, so the password never
            # passes through this process.
            argv = credential_store.keychain_prompt_argv(step.service, step.account)
            self.say(f"    $ {command_line(argv, self.windows)}")
            try:
                result = self.runner(argv, check=False)
            except OSError:
                raise InstallError("cannot run security") from None
            if result.returncode:
                raise InstallError(f"security exited with status {result.returncode}")
            self.changed.add(step.tag)
            return
        else:
            secret = self._prompt_twice(step.account)
            if not secret:
                self.warn("no password entered; rerun the installer to store it later")
                return
        try:
            credential_store.store_secret(
                step.store, step.service, step.account, secret, dict(step.locations)
            )
        except credential_store.CredentialStoreError as error:
            hint = ""
            if step.store == "libsecret":
                hint = (
                    "; secret-tool needs a running, unlocked Secret Service such as "
                    "gnome-keyring (on headless hosts use --credential-store pass)"
                )
            raise InstallError(f"{error}{hint}") from None
        self.changed.add(step.tag)

    def _prompt_twice(self, account: str) -> str:
        label = f"Bridge IMAP password for {account} (input hidden, Enter to skip): "
        first = self._hidden(label)
        if not first:
            return ""
        if self._hidden("Repeat the Bridge password: ") != first:
            raise InstallError("the two entries differ")
        return first

    def _hidden(self, label: str) -> str:
        """getpass warns and then reads with echo when it cannot hide input; never echo."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                return self.prompt(label)
            except getpass.GetPassWarning:
                raise InstallError(
                    "this terminal cannot hide the password, so the installer will not read "
                    "it; rerun in a terminal, or use --skip-credentials and store it yourself"
                ) from None

    def _DeleteSecret(self, step: DeleteSecret) -> None:
        where = f"{step.store} ({step.service}, account {step.account}){_at(step.locations)}"
        self.say(f"    delete from {where}")
        if self.dry_run:
            return
        try:
            removed = credential_store.remove_secret(
                step.store, step.service, step.account, dict(step.locations)
            )
        except credential_store.CredentialStoreError as error:
            raise InstallError(str(error)) from None
        if not removed:
            self.say("    already absent")

    def _WaitForPort(self, step: WaitForPort) -> None:
        self.say(f"    connect to 127.0.0.1:{step.port} for up to {step.timeout:g} s")
        if self.dry_run:
            return
        for attempt in range(max(1, int(step.timeout / 0.5))):
            if attempt:
                self.sleep(0.5)
            if self.port_open(step.port):
                self.say("    ok: listening")
                return
        hint = f"; see {step.hint}" if step.hint else ""
        self.warn(f"nothing listens on 127.0.0.1:{step.port} yet{hint}")

    # Guided setup -------------------------------------------------------------

    def _BridgeLogin(self, step: BridgeLogin) -> None:
        """Bridge's CLI inherits this terminal and takes the Proton password itself; this
        process never sees it. It never opens beside a running Bridge. Never fatal: the
        account may already be logged in."""
        self.say(f"    $ {command_line(step.argv, self.windows)}")
        service = step.service
        if self.dry_run:
            self.say("    (asks first, on a terminal; in the CLI use login, info and exit)")
            if service:
                self.say("    (if the installer's Bridge service runs, it asks to do this first:)")
                self.say(f"    $ {command_line(service.stop, self.windows)}")
                self.say("    (and this afterwards:)")
                self.say(f"    $ {command_line(service.start, self.windows)}")
            return
        if self.assume_yes or not self.terminal():
            self.say("    not opened: it needs a person at a terminal; the note above says how")
            return
        if service and not self._proven_stopped(service.stopped):
            self._login_around_service(step, service)
            return
        another = self._another_bridge(step)
        if another:
            self.say(f"    not opened: {another}")
            return
        answer = self.ask(
            "    Open Bridge's CLI here to log in? Answer n if this account is already "
            "logged in. [Y/n] "
        )
        if answer.strip().lower() not in {"n", "no"}:
            self._open_bridge_cli(step)

    def _another_bridge(self, step: BridgeLogin) -> str:
        """What shows a Bridge is, or may be, running; "" when nothing does. A free port
        shows nothing by itself: a Bridge still starting up holds no port yet."""
        if self.port_open(step.imap_port):
            return (
                f"127.0.0.1:{step.imap_port} is in use, most likely by a running Bridge, and its "
                "CLI cannot run beside one"
            )
        if not step.processes:
            return ""
        try:
            result = self.runner(
                list(step.processes), check=False, capture_output=True, text=True, timeout=_TIMEOUT
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 1:
            return ""
        if result is not None and result.returncode == 0:
            return "a Bridge is running, and its CLI cannot run beside one"
        return "cannot check whether a Bridge is running, and its CLI cannot run beside one"

    def _login_around_service(self, step: BridgeLogin, service: BridgeService) -> None:
        """The installer's own Bridge runs: stop it for the CLI and always start it again.
        Whether an account is logged in cannot be seen from here: Bridge listens either way.
        So Enter follows the stored password, which leaves a healthy rerun alone."""
        lead = (
            "    The installer's Bridge service is running, and Bridge's CLI cannot run beside it."
        )
        if step.password_stored:
            answer = self.ask(
                f"{lead} A Bridge password is stored, so an account is most likely logged in. To "
                "log in again, or to read the password with `info`: stop the service, open the "
                "CLI here and start the service again afterwards? [y/N] "
            )
            agreed = answer.strip().lower() in {"y", "yes"}
        else:
            answer = self.ask(
                f"{lead} No Bridge password is stored yet, so an account most likely still has "
                "to log in. Stop the service, open the CLI here and start the service again "
                "afterwards? [Y/n] "
            )
            agreed = answer.strip().lower() not in {"n", "no"}
        if not agreed:
            return
        try:
            still = self._stop_bridge_service(step, service)
            if still:
                self.warn(f"Bridge's CLI was not opened: {still}")
            else:
                self._open_bridge_cli(step)
        finally:
            self._start_bridge_service(service)

    def _stop_bridge_service(self, step: BridgeLogin, service: BridgeService) -> str:
        """Stop it and wait for it to go; what still shows a Bridge, or "" when nothing does."""
        self.say(f"    $ {command_line(service.stop, self.windows)}")
        try:
            self.runner(
                list(service.stop),
                check=False,
                capture_output=True,
                text=True,
                timeout=_VERIFY_TIMEOUT,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Whether it stopped is for the query below, whatever this one answered.
        if not self._proven_stopped(service.stopped):
            return "the installer's Bridge service did not stop"
        still = ""
        for attempt in range(_BRIDGE_STOP_CHECKS):
            if attempt:
                self.sleep(0.5)
            still = self._another_bridge(step)
            if not still:
                break
        return still

    def _start_bridge_service(self, service: BridgeService) -> None:
        """Only warns: the plan's later steps and self-check show a Bridge that stays down."""
        command = command_line(service.start, self.windows)
        self.say(f"    $ {command}")
        for attempt in range(_BRIDGE_START_RETRIES + 1):
            if attempt:
                self.sleep(1.0)
            try:
                result = self.runner(
                    list(service.start),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT,
                )
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                return
        # A service that never stopped refuses a second load, and needs nothing.
        if self._proven_stopped(service.stopped):
            self.warn(f"cannot start Bridge's service again; run `{command}`")

    def _open_bridge_cli(self, step: BridgeLogin) -> None:
        self.say(
            "    In the CLI use `login`, then `info` (it shows the IMAP username and password "
            "the next step asks for), then `exit`."
        )
        try:
            result = self.runner(list(step.argv), check=False)
        except OSError:
            self.warn(f"cannot run {step.argv[0]}; log in to Bridge by hand")
            return
        if result.returncode:
            self.warn(f"Bridge's CLI exited with status {result.returncode}")

    def _SelfCheck(self, step: SelfCheck) -> None:
        self.say(
            f"    POST http://127.0.0.1:{step.port}/mcp without and with the stored token (never "
            f"printed), then {selfcheck.PROBE_TOOL} for INBOX (its result is not shown)"
        )
        if self.dry_run:
            return
        try:
            token = credential_store.lookup(
                step.store, step.service, step.account, dict(step.locations)
            )
            self.check_service(step.port, token, self.say)
        except (credential_store.CredentialStoreError, selfcheck.SelfCheckError) as error:
            hint = f"; see {step.hint}" if step.hint else ""
            raise InstallError(f"{error}{hint}") from None


def primary_keys_from_colons(text: str) -> list[tuple[str, str, str]]:
    """(record type, fingerprint, validity) of each primary key in `gpg --with-colons`.

    A fingerprint counts only from the fpr record directly after its pub or sec
    record, so a subkey carrying the pinned key's material never passes as primary.
    """
    keys: list[tuple[str, str, str]] = []
    expecting = False
    for line in text.splitlines():
        fields = line.split(":")
        if fields[0] in {"pub", "sec"}:
            keys.append((fields[0], "", fields[1] if len(fields) > 1 else ""))
            expecting = True
        elif fields[0] == "fpr" and expecting:
            kind, _fingerprint, validity = keys[-1]
            keys[-1] = (kind, fields[9].upper() if len(fields) > 9 else "", validity)
            expecting = False
        else:
            expecting = False
    return keys


def check_pinned_key(colons: str, fingerprint: str) -> None:
    """The file must hold exactly one primary key, and it must be the pinned one."""
    keys = primary_keys_from_colons(colons)
    if len(keys) != 1:
        raise InstallError(f"expected exactly one primary key in the file, found {len(keys)}")
    ((kind, found, validity),) = keys
    if kind != "pub":
        raise InstallError("the key file holds secret key material")
    if found != fingerprint.upper():
        raise InstallError(f"the primary key is not the pinned key {fingerprint}")
    if validity in _UNUSABLE_VALIDITY:
        raise InstallError("the pinned key is revoked, expired or invalid")


def rpm_check_commands(dbpath: str, key: str, package: str) -> list[list[str]]:
    """Create the database, check it trusts nothing, import the key, check again."""
    base = ["rpm", "--dbpath", dbpath, "--define", f"_keyringpath {dbpath}/pubkeys/"]
    return [
        [*base, "--initdb"],
        [*base, "--checksig", package],
        [*base, "--import", key],
        [*base, "--checksig", package],
    ]


def debsig_check_commands(root: str, key_id: str, key: str, package: str) -> list[list[str]]:
    """Check the empty directories verify nothing, convert and list the key, verify."""
    keyring = f"{root}/keyrings/{key_id}/{DEBSIG_KEYRING}"
    verify = [
        "debsig-verify",
        "--policies-dir",
        f"{root}/policies",
        "--keyrings-dir",
        f"{root}/keyrings",
        package,
    ]
    return [
        verify,
        ["gpg", "--batch", "--yes", "--dearmor", "--output", keyring, key],
        ["gpg", "--batch", "--with-colons", "--show-keys", keyring],
        list(verify),
    ]


def check_debsig_policy(data: bytes, key_id: str) -> None:
    if len(data) > MAX_POLICY_BYTES:
        raise InstallError("the policy is too large")
    if b"<!ENTITY" in data:
        raise InstallError("the policy must not declare entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise InstallError("the policy is not valid XML") from None
    ids = [element.get("id") for element in root.iter() if element.get("id") is not None]
    files = [element.get("File") for element in root.iter() if element.get("File") is not None]
    if not ids or any(value.upper() != key_id.upper() for value in ids):
        raise InstallError("the policy names a key other than the pinned one")
    if not files or any(value != DEBSIG_KEYRING for value in files):
        raise InstallError("the policy names an unexpected keyring file")


def _copy_files(distribution: metadata.Distribution) -> dict[str, str]:
    """Each file a wheel installed, with the hash of what is on disk, not what RECORD
    says. Scripts, bytecode and an installer's own files differ between equal copies."""
    files = {}
    for file in distribution.files or ():
        parts = file.parts
        if parts[0] == ".." or "__pycache__" in parts:
            continue
        if parts[0].endswith(".dist-info") and file.name not in _COMPARED_METADATA:
            continue
        files[file.as_posix()] = hashlib.sha256(file.read_binary()).hexdigest()
    return files


def _at(locations: Sequence[tuple[str, str]]) -> str:
    return "".join(f", {name}={value}" for name, value in locations)


def _output(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _make_dirs(*paths: Path) -> None:
    try:
        for path in paths:
            path.mkdir(mode=0o700)
    except OSError:
        raise InstallError("cannot create the temporary debsig directories") from None


def _indent(text: str) -> str:
    return "\n".join(f"    | {line}" for line in text.rstrip().splitlines())


def _kept_scratch(what: str, path: str) -> str:
    return f"    {what} {path} stays: the installer never deletes a directory with content"


def _new_file(directory: Path, prefix: str, suffix: str, data: bytes, mode: int) -> Path:
    """Write data to a file under a fresh name, created exclusively, so nothing that
    was there is touched. A file that cannot be finished stays, and the error names it."""
    try:
        descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    except OSError:
        raise InstallError(f"cannot create a file in {directory}") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
    except OSError:
        raise InstallError(f"cannot write {name}, which stays") from None
    return Path(name)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = _new_file(path.parent, f".{path.name}.", ".tmp", data, mode)
    try:
        os.replace(temporary, path)
    except OSError:
        raise InstallError(f"cannot write {path}; the new content stays in {temporary}") from None
