"""Installer executor: dry runs change nothing, files stay owned, secrets stay hidden."""

import getpass
import hashlib
import io
import os
import shlex
import subprocess
import sys

import pytest

from proton_mail_mcp import credential_store
from proton_mail_mcp.installer import execute
from proton_mail_mcp.installer import probe as probe_module
from proton_mail_mcp.installer.detect import simulated
from proton_mail_mcp.installer.execute import (
    Executor,
    InstallError,
    check_debsig_policy,
    debsig_check_commands,
    primary_keys_from_colons,
)
from proton_mail_mcp.installer.probe import HostProbe
from proton_mail_mcp.installer.steps import (
    DIR_MARKER,
    MARKER,
    CheckDebSignature,
    CheckKey,
    CheckPolicy,
    CheckRpmSignature,
    CheckStopped,
    Command,
    DeleteSecret,
    Download,
    EnsureDir,
    Note,
    RemovePath,
    StoreSecret,
    WaitForPort,
    WriteFile,
)

KEY_ID = "E2C75D68E6234B07"
FINGERPRINT = "D51E64D3E63EDC3EEF7864CEE2C75D68E6234B07"
POLICY = f"""<?xml version="1.0"?>
<Policy xmlns="https://www.debian.org/debsig/1.0/">
  <Origin Name="Proton" id="{KEY_ID}" Description="Bridge"/>
  <Selection><Required Type="origin" File="debsig.gpg" id="{KEY_ID}"/></Selection>
  <Verification MinOptional="0">
    <Required Type="origin" File="debsig.gpg" id="{KEY_ID}"/>
  </Verification>
</Policy>
""".encode()
# The shape of `gpg --with-colons --show-keys bridge_pubkey.gpg` for Proton's key.
COLONS = (
    f"pub:-:4096:1:{KEY_ID}:1513961953:::-:::scESC::::::23::0:\n"
    f"fpr:::::::::{FINGERPRINT}:\n"
    "uid:-::::1706890627::D6162B5B91BBB2F516A918831214A0EB52A6BF04::Proton Technologies AG "
    "(ProtonMail Bridge developers) <bridge@protonmail.ch>::::::::::0:\n"
    "sub:-:4096:1:C65A295E99026E3D:1513961953::::::e::::::23:\n"
    "fpr:::::::::E8BF6C516292215470BB2044C65A295E99026E3D:\n"
)
ATTACKER = "A11CE0000000000000000000000000000000BAD1"


def attacker_key(sub_id, sub_fingerprint):
    """An attacker's primary key that carries some other key material as a subkey."""
    return (
        f"pub:-:4096:1:{ATTACKER[-16:]}:1700000000:::-:::scESC::::::23::0:\n"
        f"fpr:::::::::{ATTACKER}:\n"
        "uid:-::::1700000000::0000::Proton Technologies AG <bridge@protonmail.ch>::::::::::0:\n"
        f"sub:-:4096:1:{sub_id}:1513961953::::::es::::::23:\n"
        f"fpr:::::::::{sub_fingerprint}:\n"
    )


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        result = (
            self.results.pop(0) if self.results else subprocess.CompletedProcess(argv, 0, "", "")
        )
        if isinstance(result, BaseException):
            raise result
        return result


def done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def executor(**kwargs):
    out = io.StringIO()
    kwargs.setdefault("runner", Runner())
    kwargs.setdefault("opener", lambda url: pytest.fail("network"))
    kwargs.setdefault("prompt", lambda label: pytest.fail("prompted"))
    return Executor(dry_run=kwargs.pop("dry_run", False), out=out, windows=False, **kwargs), out


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(credential_store, "store_secret", lambda *a: pytest.fail("stored"))
    monkeypatch.setattr(credential_store, "remove_secret", lambda *a: pytest.fail("removed"))
    existing = tmp_path / "owned.txt"
    existing.write_text(MARKER)
    steps = [
        EnsureDir(str(tmp_path / "dir"), "dir"),
        WriteFile(str(tmp_path / "dir" / "file"), f"{MARKER}\n", "file", tag="t"),
        Command(("rm", "-rf", "/"), "dangerous", sudo=True),
        Download("https://example.test/x", str(tmp_path / "x"), "download"),
        CheckKey(str(tmp_path / "key"), FINGERPRINT, "key"),
        CheckRpmSignature(str(tmp_path / "x.rpm"), str(tmp_path / "key"), "rpm"),
        CheckDebSignature(
            str(tmp_path / "x.deb"),
            str(tmp_path / "key"),
            str(tmp_path / "pol"),
            KEY_ID,
            FINGERPRINT,
            "deb",
        ),
        CheckPolicy(str(tmp_path / "pol"), KEY_ID, "policy"),
        StoreSecret("libsecret", "svc", "acct", "prompt", "secret"),
        DeleteSecret("libsecret", "svc", "acct", "forget"),
        CheckStopped(("systemctl", "--user", "show", "x"), "check", "hint", states=("inactive",)),
        RemovePath(str(existing), "remove"),
        RemovePath(str(tmp_path), "remove the directory", directory=True),
        WaitForPort(1, "port"),
        Note("a note"),
    ]
    runner = Runner()
    run, out = executor(dry_run=True, runner=runner, port_open=lambda port: pytest.fail("port"))
    run.run(steps)
    assert runner.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["owned.txt"]
    text = out.getvalue()
    assert "[3/14] [dry run] dangerous" in text and "$ sudo rm -rf /" in text
    assert "--dbpath '<temporary directory>'" in text
    assert "--policies-dir '<temporary directory>/policies'" in text
    assert "  - a note" in text


def test_write_file_is_private_atomic_and_idempotent(tmp_path):
    path = tmp_path / "nested" / "settings.env"
    step = WriteFile(str(path), f"# {MARKER}\nA=1\n", "write", tag="env")
    run, out = executor()
    run.run([step])
    assert path.read_text() == f"# {MARKER}\nA=1\n"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert run.changed == {"env"}
    again, out = executor()
    again.run([step])
    assert again.changed == set() and "unchanged" in out.getvalue()
    assert [p.name for p in path.parent.iterdir()] == ["settings.env"]


def test_utf16_task_files_compare_and_count_as_managed(tmp_path):
    path = tmp_path / "task.xml"
    step = WriteFile(str(path), f"<Task>{MARKER}</Task>", "task", encoding="utf-16")
    executor()[0].run([step])
    assert path.read_bytes()[:2] in {b"\xff\xfe", b"\xfe\xff"}
    run, out = executor()
    run.run([step])
    assert "unchanged" in out.getvalue()
    run.run([RemovePath(str(path), "remove")])
    assert not path.exists()


def test_unmanaged_files_need_force_and_keep_a_backup(tmp_path):
    path = tmp_path / "com.proton-mail-mcp.plist"
    path.write_text("hand-written plist with a token")
    # --force wrote over an earlier backup, which may be the only copy.
    earlier = tmp_path / "com.proton-mail-mcp.plist.bak"
    earlier.write_text("an earlier backup")
    step = WriteFile(str(path), f"<!-- {MARKER} -->", "write")
    for dry_run in (True, False):
        with pytest.raises(InstallError, match="--force"):
            executor(dry_run=dry_run)[0].run([step])
    assert path.read_text() == "hand-written plist with a token"
    run, out = executor(force=True)
    run.run([step])
    assert earlier.read_text() == "an earlier backup"
    (backup,) = set(tmp_path.glob("*.bak")) - {earlier}
    assert backup.name.startswith("com.proton-mail-mcp.plist.")
    assert backup.read_text() == "hand-written plist with a token"
    assert f"kept the previous file as {backup}" in out.getvalue()
    assert path.read_text() == f"<!-- {MARKER} -->"
    if os.name == "posix":
        assert backup.stat().st_mode & 0o777 == 0o600


def test_remove_path_only_removes_installer_files(tmp_path):
    owned = tmp_path / "owned.service"
    owned.write_text(f"# {MARKER}\n")
    foreign = tmp_path / "foreign.service"
    foreign.write_text("# mine\n")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / DIR_MARKER).write_text(MARKER)
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep").write_text("x")
    run, out = executor()
    run.run(
        [
            RemovePath(str(owned), "owned"),
            RemovePath(str(foreign), "foreign"),
            RemovePath(str(venv), "venv", directory=True),
            RemovePath(str(empty), "empty", directory=True),
            RemovePath(str(full), "full", directory=True),
            RemovePath(str(tmp_path / "missing"), "missing"),
        ]
    )
    assert not owned.exists() and not empty.exists()
    # Round 7: a marked venv is a directory with content like any other; it stays.
    assert foreign.exists() and (venv / DIR_MARKER).exists() and (full / "keep").exists()
    assert len(run.warnings) == 1


def test_commands_follow_sudo_change_tags_and_failures():
    runner = Runner(done(), done(), done(1, "", "boom"), done(3))
    run, out = executor(runner=runner, sudo=("sudo", "-k"))
    run.run(
        [
            Command(("apt-get", "update"), "update", sudo=True, marks=("pkg",)),
            Command(("systemctl", "restart", "a"), "restart a", when_changed=("pkg",)),
            Command(("systemctl", "restart", "b"), "restart b", when_changed=("unit",)),
            Command(("launchctl", "bootout", "x"), "bootout", allow_failure=True),
        ]
    )
    assert [call[0] for call in runner.calls] == [
        ["sudo", "-k", "apt-get", "update"],
        ["systemctl", "restart", "a"],
        ["launchctl", "bootout", "x"],
    ]
    assert "skipped: nothing it depends on changed (unit)" in out.getvalue()
    failing, out = executor(runner=Runner(done(2, "", "diagnostic text")))
    with pytest.raises(InstallError, match=r"Step 1 \(fail\) failed: exit status 2"):
        failing.run([Command(("false",), "fail")])
    assert "diagnostic text" in out.getvalue()


def test_interactive_commands_inherit_the_terminal_and_env_is_added(monkeypatch):
    runner = Runner()
    run, _out = executor(runner=runner)
    run.run(
        [
            Command(("apt-get", "install", "-y", "x"), "install", interactive=True),
            Command(
                ("powershell", "-Command", "x"), "check", env=(("PMM_VERIFY_PATH", "C:\\b.exe"),)
            ),
        ]
    )
    (_, interactive), (_, checked) = runner.calls
    assert interactive["capture_output"] is False and interactive["env"] is None
    assert checked["capture_output"] is True
    assert checked["env"]["PMM_VERIFY_PATH"] == "C:\\b.exe"


def test_expected_output_and_retries():
    runner = Runner(done(1), done(0, "pkg.rpm: digests signatures OK\n"))
    run, out = executor(runner=runner, sleep=lambda seconds: None)
    run.run(
        [
            Command(
                ("rpm", "--checksig", "pkg.rpm"),
                "check",
                expect="digests signatures OK",
                retries=1,
            )
        ]
    )
    assert "ok: digests signatures OK" in out.getvalue()
    bad, _out = executor(runner=Runner(done(0, "pkg.rpm: digests SIGNATURES NOT OK\n")))
    with pytest.raises(InstallError, match="does not contain"):
        bad.run(
            [Command(("rpm", "--checksig", "pkg.rpm"), "check", expect="digests signatures OK")]
        )


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def test_download_writes_a_private_file(tmp_path):
    target = tmp_path / "bridge.deb"
    run, out = executor(opener=lambda url: Response(b"package-bytes", url))
    run.run([Download("https://proton.me/download/bridge/x.deb", str(target), "download")])
    assert target.read_bytes() == b"package-bytes"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    assert "13 bytes" in out.getvalue()


@pytest.mark.skipif(sys.platform == "win32", reason="names POSIX paths")
def test_download_refuses_plain_http_and_oversized_files(tmp_path, monkeypatch):
    target = tmp_path / "bridge.deb"
    run, _out = executor(opener=lambda url: Response(b"x", "http://mirror.example/x.deb"))
    with pytest.raises(InstallError, match="left HTTPS"):
        run.run([Download("https://proton.me/x.deb", str(target), "download")])
    with pytest.raises(InstallError, match="HTTPS"):
        run.run([Download("http://proton.me/x.deb", str(target), "download")])
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(execute, "MAX_DOWNLOAD_BYTES", 4)
    run, _out = executor(opener=lambda url: Response(b"too large", url))
    with pytest.raises(InstallError, match="larger") as caught:
        run.run([Download("https://proton.me/x.deb", str(target), "download")])
    # What this run created stays; the error says how to remove it.
    assert f"the partial file stays: remove it with `rm -f -- {target}`" in str(caught.value)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bridge.deb"]


@pytest.mark.skipif(sys.platform == "win32", reason="names POSIX paths")
def test_r8_no_file_already_there_is_replaced_or_deleted(tmp_path):
    # A stale .part, a taken temporary name and an earlier
    # download were unlinked or replaced although this run had not created them.
    target = tmp_path / "bridge.deb"
    target.write_bytes(b"not the installer's")
    stale = {
        name: f"not the installer's {name}".encode()
        for name in ("bridge.deb.part", f".settings.env.{os.getpid()}.tmp")
    }
    for name, data in stale.items():
        (tmp_path / name).write_bytes(data)
    run, _out = executor(opener=lambda url: Response(b"package-bytes", url))
    with pytest.raises(InstallError, match="already exists") as caught:
        run.run([Download("https://proton.me/x.deb", str(target), "download")])
    assert f"remove it with `rm -f -- {target}`, then rerun" in str(caught.value)
    run.run([WriteFile(str(tmp_path / "settings.env"), f"# {MARKER}\n", "write")])
    assert target.read_bytes() == b"not the installer's"
    assert {name: (tmp_path / name).read_bytes() for name in stale} == stale
    assert {path.name for path in tmp_path.iterdir()} == {*stale, "bridge.deb", "settings.env"}


def test_redirects_away_from_https_are_refused():
    handler = execute._HTTPSOnlyRedirects()
    with pytest.raises(InstallError, match="HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example/x")


def test_primary_keys_come_only_from_pub_records():
    assert primary_keys_from_colons(COLONS) == [("pub", FINGERPRINT, "-")]
    assert primary_keys_from_colons(attacker_key(KEY_ID, FINGERPRINT)) == [("pub", ATTACKER, "-")]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (COLONS, None),
        # Proton's key material, or just its long ID, reused as an attacker's subkey.
        (attacker_key(KEY_ID, FINGERPRINT), "not the pinned key"),
        (attacker_key(KEY_ID, "0" * 24 + KEY_ID), "not the pinned key"),
        # A different primary key whose long ID collides with the published one.
        (COLONS.replace(FINGERPRINT, "0" * 24 + KEY_ID), "not the pinned key"),
        (COLONS + attacker_key("C65A295E99026E3D", ATTACKER), "exactly one primary key"),
        (attacker_key("1" * 16, "1" * 40) + COLONS, "exactly one primary key"),
        (COLONS + COLONS, "exactly one primary key"),
        ("", "found 0"),
        (COLONS.replace("pub:", "sec:"), "secret key material"),
        (COLONS.replace("pub:-:", "pub:r:"), "revoked"),
        (COLONS.replace("pub:-:", "pub:e:"), "expired"),
        # The fingerprint must directly follow its pub record.
        (COLONS.replace(f"fpr:::::::::{FINGERPRINT}:\n", ""), "not the pinned key"),
    ],
)
def test_check_key_requires_the_pinned_fingerprint_as_the_only_primary(output, message):
    runner = Runner(done(0, output))
    run, _out = executor(runner=runner)
    step = CheckKey("/tmp/key.gpg", FINGERPRINT, "key")
    if message:
        with pytest.raises(InstallError, match=message):
            run.run([step])
    else:
        run.run([step])
    assert runner.calls[0][0] == ["gpg", "--batch", "--with-colons", "--show-keys", "/tmp/key.gpg"]


def rpm_files(tmp_path):
    package = tmp_path / "bridge.rpm"
    key = tmp_path / "bridge_pubkey.gpg"
    package.write_bytes(b"rpm")
    key.write_bytes(b"key")
    return package, key, CheckRpmSignature(str(package), str(key), "verify")


def kept_scratch(tmp_path, prefix, out):
    """The one scratch directory a check made; it stays, and the output names it."""
    (scratch,) = [path for path in tmp_path.iterdir() if path.name.startswith(prefix)]
    assert scratch.is_dir()
    assert f"{scratch} stays: the installer never deletes a directory with content" in out
    return scratch


SIGNED = done(0, "bridge.rpm: digests signatures OK\n")
NOT_SIGNED = done(1, "bridge.rpm: digests SIGNATURES NOT OK\n")


def test_rpm_is_verified_against_a_temporary_database_holding_only_the_key(tmp_path):
    package, key, step = rpm_files(tmp_path)
    runner = Runner(done(), NOT_SIGNED, done(), SIGNED)
    run, out = executor(runner=runner)
    run.run([step])
    calls = [argv for argv, _kwargs in runner.calls]
    dbpath = calls[0][2]
    assert os.path.dirname(dbpath) == str(tmp_path) and dbpath != "/var/lib/rpm"
    base = ["rpm", "--dbpath", dbpath, "--define", f"_keyringpath {dbpath}/pubkeys/"]
    assert calls == [
        [*base, "--initdb"],
        [*base, "--checksig", str(package)],
        [*base, "--import", str(key)],
        [*base, "--checksig", str(package)],
    ]
    assert all(kwargs["capture_output"] for _argv, kwargs in runner.calls)
    # The scratch database stays; nothing deletes a populated directory.
    assert str(kept_scratch(tmp_path, ".rpmdb-", out.getvalue())) == dbpath
    assert {path.name for path in tmp_path.iterdir()} == {
        os.path.basename(dbpath),
        "bridge.rpm",
        "bridge_pubkey.gpg",
    }
    assert "ok: digests signatures OK with only the pinned key" in out.getvalue()


@pytest.mark.parametrize(
    ("results", "message", "calls"),
    [
        # Verified before any key was imported: system keys leak in, so fail closed.
        ([done(), SIGNED], "not isolated", 2),
        # Signed by some other key, or not signed at all.
        ([done(), NOT_SIGNED, done(), NOT_SIGNED], "not signed by the pinned key", 4),
        (
            [done(), done(0, "bridge.rpm: digests OK\n"), done(), done(0, "digests OK\n")],
            "pinned",
            4,
        ),
        ([done(1, "", "error: cannot create")], "temporary database", 1),
        ([done(), NOT_SIGNED, done(1, "", "error: bad key")], "cannot import", 3),
        ([OSError("no rpm")], "cannot run rpm", 1),
    ],
)
def test_rpm_verification_fails_closed(tmp_path, results, message, calls):
    _package, _key, step = rpm_files(tmp_path)
    runner = Runner(*results)
    run, out = executor(runner=runner)
    with pytest.raises(InstallError, match=message):
        run.run([step])
    assert len(runner.calls) == calls
    scratch = kept_scratch(tmp_path, ".rpmdb-", out.getvalue())
    assert {path.name for path in tmp_path.iterdir()} == {
        scratch.name,
        "bridge.rpm",
        "bridge_pubkey.gpg",
    }


def deb_files(tmp_path, policy=POLICY):
    package = tmp_path / "bridge.deb"
    key = tmp_path / "bridge_pubkey.gpg"
    pol = tmp_path / "bridge.pol"
    package.write_bytes(b"deb")
    key.write_bytes(b"key")
    pol.write_bytes(policy)
    step = CheckDebSignature(str(package), str(key), str(pol), KEY_ID, FINGERPRINT, "verify")
    return package, key, step


DEB_FILES = ["bridge.deb", "bridge.pol", "bridge_pubkey.gpg"]
NO_POLICY = done(10, "", "debsig: Origin Signature check failed. No applicable policy found.\n")
DEB_SIGNED = done(0, "debsig: Verified package from 'Proton Technologies AG'\n")
DEB_NOT_SIGNED = done(13, "", "debsig: Signature verification failed\n")


class DebsigHost(Runner):
    """Records which files the temporary debsig directories hold at each command."""

    def __init__(self, *results):
        super().__init__(*results)
        self.trees = []

    def __call__(self, argv, **kwargs):
        root = argv[argv.index("--output") + 1].split("/keyrings/")[0] if "--output" in argv else ""
        if argv[0] == "debsig-verify":
            root = os.path.dirname(argv[argv.index("--policies-dir") + 1])
        if root:
            # Compared with "/"-separated names on every platform.
            self.trees.append(
                sorted(
                    os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/")
                    for base, dirs, files in os.walk(root)
                    for name in dirs + files
                )
            )
        if "--dearmor" in argv:
            with open(argv[argv.index("--output") + 1], "wb") as handle:
                handle.write(b"dearmored")
        return super().__call__(argv, **kwargs)


def test_deb_is_verified_against_temporary_directories_holding_only_proton(tmp_path):
    package, key, step = deb_files(tmp_path)
    runner = DebsigHost(NO_POLICY, done(), done(0, COLONS), DEB_SIGNED)
    run, out = executor(runner=runner)
    run.run([step])
    calls = [argv for argv, _kwargs in runner.calls]
    root = os.path.dirname(calls[0][2])
    assert os.path.dirname(root) == str(tmp_path)
    assert os.path.basename(root).startswith(".debsig-")
    assert calls == debsig_check_commands(root, KEY_ID, str(key), str(package))
    verify = [
        "debsig-verify",
        "--policies-dir",
        f"{root}/policies",
        "--keyrings-dir",
        f"{root}/keyrings",
        str(package),
    ]
    keyring = f"{root}/keyrings/{KEY_ID}/debsig.gpg"
    assert calls == [
        verify,
        ["gpg", "--batch", "--yes", "--dearmor", "--output", keyring, str(key)],
        ["gpg", "--batch", "--with-colons", "--show-keys", keyring],
        verify,
    ]
    # Empty directories first, then exactly Proton's policy and the pinned key.
    proton = [
        "keyrings",
        f"keyrings/{KEY_ID}",
        f"keyrings/{KEY_ID}/debsig.gpg",
        "policies",
        f"policies/{KEY_ID}",
        f"policies/{KEY_ID}/bridge.pol",
    ]
    assert runner.trees[0] == ["keyrings", "policies"]
    assert runner.trees[-1] == proton
    assert all(kwargs["capture_output"] for _argv, kwargs in runner.calls)
    assert not any("sudo" in argv for argv in calls)
    assert str(kept_scratch(tmp_path, ".debsig-", out.getvalue())) == root
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_file()) == DEB_FILES
    assert "ok: Verified package from with only the pinned policy and key" in out.getvalue()


def test_the_temporary_policy_is_the_checked_policy(tmp_path):
    _package, _key, step = deb_files(tmp_path)
    seen = {}

    class Host(DebsigHost):
        def __call__(self, argv, **kwargs):
            if len(self.calls) == 3:
                root = os.path.dirname(argv[2])
                with open(f"{root}/policies/{KEY_ID}/bridge.pol", "rb") as handle:
                    seen["policy"] = handle.read()
                seen["mode"] = os.stat(f"{root}/policies").st_mode & 0o777
            return super().__call__(argv, **kwargs)

    run, _out = executor(runner=Host(NO_POLICY, done(), done(0, COLONS), DEB_SIGNED))
    run.run([step])
    assert seen["policy"] == POLICY
    # Windows has no POSIX permission bits to compare.
    if os.name == "posix":
        assert seen["mode"] == 0o700


@pytest.mark.parametrize(
    ("results", "message", "calls"),
    [
        # Verified with empty directories: system policies or keys leak in.
        ([DEB_SIGNED], "not isolated", 1),
        ([done(0, "")], "not isolated", 1),
        ([done(1, "debsig: Verified package from 'Other'\n")], "not isolated", 1),
        ([NO_POLICY, done(2, "", "gpg: no valid OpenPGP data found.\n")], "cannot convert", 2),
        ([NO_POLICY, done(), done(2, "", "gpg: read error\n")], "cannot read the converted", 3),
        # The keyring debsig-verify would read holds something besides the pinned key.
        (
            [NO_POLICY, done(), done(0, COLONS + attacker_key("1111222233334444", "1" * 40))],
            "exactly one primary key",
            3,
        ),
        (
            [NO_POLICY, done(), done(0, attacker_key(KEY_ID, FINGERPRINT))],
            "not the pinned key",
            3,
        ),
        # Signed by some other origin, or not signed at all.
        ([NO_POLICY, done(), done(0, COLONS), DEB_NOT_SIGNED], "not signed by the pinned key", 4),
        ([NO_POLICY, done(), done(0, COLONS), done(0, "")], "not signed by the pinned key", 4),
        ([OSError("no debsig-verify")], "cannot run debsig-verify", 1),
        ([NO_POLICY, OSError("no gpg")], "cannot run gpg", 2),
    ],
)
def test_deb_verification_fails_closed(tmp_path, results, message, calls):
    _package, _key, step = deb_files(tmp_path)
    runner = DebsigHost(*results)
    run, out = executor(runner=runner)
    with pytest.raises(InstallError, match=message):
        run.run([step])
    assert len(runner.calls) == calls
    kept_scratch(tmp_path, ".debsig-", out.getvalue())
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_file()) == DEB_FILES


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        (POLICY.replace(KEY_ID.encode(), b"0000000000000000", 1), "other than"),
        (POLICY.replace(b"debsig.gpg", b"other.gpg", 1), "keyring file"),
        (None, "cannot read the policy"),
    ],
    ids=["other-key", "other-keyring", "missing"],
)
def test_deb_verification_rechecks_the_policy_it_installs(tmp_path, policy, message):
    _package, _key, step = deb_files(tmp_path, policy or POLICY)
    if policy is None:
        os.unlink(step.policy)
    runner = DebsigHost()
    run, _out = executor(runner=runner)
    with pytest.raises(InstallError, match=message):
        run.run([step])
    assert runner.calls == []
    assert not any(path.name.startswith(".debsig-") for path in tmp_path.iterdir())


def test_debsig_policy_must_name_only_the_pinned_key():
    check_debsig_policy(POLICY, KEY_ID)
    with pytest.raises(InstallError, match="other than"):
        check_debsig_policy(POLICY.replace(KEY_ID.encode(), b"0000000000000000", 1), KEY_ID)
    with pytest.raises(InstallError, match="keyring file"):
        check_debsig_policy(POLICY.replace(b"debsig.gpg", b"other.gpg", 1), KEY_ID)
    with pytest.raises(InstallError, match="entities"):
        check_debsig_policy(b'<!DOCTYPE x [<!ENTITY a "b">]><Policy/>', KEY_ID)
    with pytest.raises(InstallError, match="valid XML"):
        check_debsig_policy(b"<Policy", KEY_ID)


def test_generated_token_is_stored_but_never_printed(monkeypatch):
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    run, out = executor()
    run.run([StoreSecret("pass", "proton-mail-mcp-token", "you@example.com", "generate", "token")])
    ((store, service, account, token, locations),) = stored
    assert (store, service, account) == ("pass", "proton-mail-mcp-token", "you@example.com")
    assert locations == {}
    assert len(token) >= 43
    assert token not in out.getvalue()
    assert run.changed == {"credentials"}


def test_prompted_password_is_confirmed_and_hidden(monkeypatch):
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    answers = iter(["bridge-pass-1234567890", "bridge-pass-1234567890"])
    run, out = executor(prompt=lambda label: next(answers))
    run.run([StoreSecret("libsecret", "proton-mail-mcp", "you@example.com", "prompt", "password")])
    assert stored[0][3] == "bridge-pass-1234567890"
    assert "bridge-pass-1234567890" not in out.getvalue()
    answers = iter(["one-bridge-password-xx", "two-bridge-password-xx"] * 3)
    run, out = executor(prompt=lambda label: next(answers))
    with pytest.raises(InstallError, match="no usable Bridge password in 3 attempts") as caught:
        run.run([StoreSecret("libsecret", "proton-mail-mcp", "you@example.com", "prompt", "pw")])
    assert "differ" in out.getvalue()
    assert "bridge-password-xx" not in str(caught.value) + out.getvalue()
    run, _out = executor(prompt=lambda label: "")
    with pytest.raises(InstallError, match="none was stored"):
        run.run([StoreSecret("libsecret", "proton-mail-mcp", "you@example.com", "prompt", "pw")])
    assert len(stored) == 1 and run.changed == set()


@pytest.mark.parametrize("warn_on", [1, 2])
def test_password_prompt_never_falls_back_to_echoed_input(monkeypatch, warn_on):
    stored = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: stored.append(args))
    typed = io.StringIO("echoed-secret\n")
    monkeypatch.setattr(sys, "stdin", typed)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    prompts = []

    def prompt(label):
        prompts.append(label)
        if len(prompts) == warn_on:
            # What getpass.getpass does when it cannot turn terminal echo off.
            return getpass.fallback_getpass(label)
        return "hidden-entry-long-enough"

    run, out = executor(prompt=prompt)
    with pytest.raises(InstallError, match="cannot hide the password") as caught:
        run.run([StoreSecret("libsecret", "proton-mail-mcp", "you@example.com", "prompt", "pw")])
    assert typed.tell() == 0, "the echoed fallback read stdin"
    assert stored == [] and len(prompts) == warn_on
    assert "echoed-secret" not in str(caught.value) + out.getvalue()
    assert "--skip-credentials" in str(caught.value)


def test_keychain_password_uses_the_native_prompt(monkeypatch):
    monkeypatch.setattr(credential_store, "store_secret", lambda *a: pytest.fail("stored"))
    runner = Runner()
    run, _out = executor(runner=runner)
    run.run([StoreSecret("keychain", "proton-mail-mcp", "you@example.com", "prompt", "pw")])
    ((argv, kwargs),) = runner.calls
    assert argv == [
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-s",
        "proton-mail-mcp",
        "-a",
        "you@example.com",
        "-w",
    ]
    assert "input" not in kwargs and "capture_output" not in kwargs


def test_store_failures_surface_without_secrets(monkeypatch):
    def refuse(*args):
        raise credential_store.CredentialStoreError("Credential store refused the entry")

    monkeypatch.setattr(credential_store, "store_secret", refuse)
    run, _out = executor()
    with pytest.raises(InstallError, match="refused"):
        run.run([StoreSecret("pass", "svc", "acct", "generate", "token")])
    with pytest.raises(InstallError, match="credential-store pass"):
        run.run([StoreSecret("libsecret", "svc", "acct", "generate", "token")])


def test_delete_secret_reports_absence(monkeypatch):
    monkeypatch.setattr(credential_store, "remove_secret", lambda *args: False)
    run, out = executor()
    run.run([DeleteSecret("pass", "svc", "acct", "forget")])
    assert "already absent" in out.getvalue()


def test_pass_steps_use_the_locations_the_service_uses(monkeypatch):
    # The installing shell's PASSWORD_STORE_DIR and GNUPGHOME are not the service's.
    calls = []
    monkeypatch.setattr(credential_store, "store_secret", lambda *args: calls.append(args))
    monkeypatch.setattr(credential_store, "remove_secret", lambda *args: calls.append(args))
    located = (("PASSWORD_STORE_DIR", "/srv/pass"), ("GNUPGHOME", "/srv/gnupg"))
    run, out = executor()
    run.run(
        [
            StoreSecret("pass", "svc", "acct", "generate", "token", locations=located),
            DeleteSecret("pass", "svc", "acct", "forget", locations=located),
        ]
    )
    assert [call[-1] for call in calls] == [dict(located), dict(located)]
    text = out.getvalue()
    assert text.count("PASSWORD_STORE_DIR=/srv/pass, GNUPGHOME=/srv/gnupg") == 2
    assert calls[0][3] not in text


# Round 7: an empty directory is the only directory ever removed (R1), files go one at
# a time and only when they are marked regular files (R2), and a stop must be proven
# before anything is removed (R3).


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links need extra privileges here")


def snapshot(top):
    """Relative path -> SHA-256, link target or "dir", for everything at or below top;
    links are never followed."""
    found = {}
    pending = [top]
    while pending:
        path = pending.pop()
        name = path.relative_to(top).as_posix()
        if path.is_symlink():
            found[name] = ("link", os.readlink(path))
        elif path.is_dir():
            found[name] = "dir"
            pending.extend(path.iterdir())
        elif path.is_file():
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def test_r7_rule1_a_directory_goes_only_when_it_is_real_and_empty(tmp_path):
    # Every check in front of an rmtree had a gap. RemovePath now
    # only ever calls rmdir, so a directory with anything in it stays whole.
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / DIR_MARKER).write_text(MARKER)
    (venv / "bin" / "python").write_bytes(b"\x7fELF" * 100)
    target = tmp_path / "empty-target"
    target.mkdir()
    link = tmp_path / "link"
    symlink_or_skip(link, target)
    plain = tmp_path / "plain"
    plain.write_text(MARKER)
    empty = tmp_path / "empty"
    empty.mkdir()
    before = snapshot(tmp_path)
    for dry_run in (True, False):
        run, out = executor(dry_run=dry_run)
        run.run(
            [
                RemovePath(str(path), path.name, directory=True)
                for path in (venv, link, plain, empty, tmp_path / "missing")
            ]
        )
        text = out.getvalue()
        assert f"{venv}: kept (not empty)" in text
        assert f"{link}: kept (a symbolic link)" in text
        assert f"{plain}: kept (not a directory)" in text
        assert f"{empty}: remove" in text and f"{tmp_path / 'missing'}: not present" in text
        assert not run.warnings
    assert not empty.exists()
    del before["empty"]
    assert snapshot(tmp_path) == before


def test_r7_rule2_a_file_goes_only_when_it_is_a_marked_regular_file(tmp_path):
    owned = tmp_path / "owned.service"
    owned.write_text(f"# {MARKER}\n")
    foreign = tmp_path / "foreign.service"
    foreign.write_text("# mine\n")
    package = tmp_path / "protonmail-bridge.deb"
    package.write_bytes(b"package this run downloaded")
    real = tmp_path / "real.service"
    real.write_text(f"# {MARKER}\n")
    link = tmp_path / "linked.service"
    symlink_or_skip(link, real)
    directory = tmp_path / "directory.service"
    directory.mkdir()
    run, out = executor()
    run.run(
        [
            RemovePath(str(owned), "owned"),
            RemovePath(str(foreign), "foreign"),
            RemovePath(str(package), "package", managed_only=False),
            RemovePath(str(link), "link"),
            RemovePath(str(directory), "directory"),
        ]
    )
    assert not owned.exists() and not package.exists()
    assert foreign.read_text() == "# mine\n"
    assert link.is_symlink() and real.read_text() == f"# {MARKER}\n"
    assert directory.is_dir()
    assert run.warnings == [f"{foreign} was not written by the installer; left in place"]
    assert f"{link}: kept (a symbolic link)" in out.getvalue()
    assert f"{directory}: kept (not a regular file)" in out.getvalue()


def test_r9_a_symbolic_link_is_never_written_or_replaced(tmp_path):
    # exists() is false for a dangling link, so writing the
    # settings file replaced the link although the installer had not made it.
    real = tmp_path / "real.service"
    real.write_text(f"# {MARKER}\nold\n")
    links = {
        tmp_path / "proton-mail-mcp.env": tmp_path / "missing.env",
        tmp_path / "linked.service": real,
    }
    for link, target in links.items():
        symlink_or_skip(link, target)
    before = snapshot(tmp_path)
    for link in links:
        step = WriteFile(str(link), f"# {MARKER}\nnew\n", "write")
        for dry_run, force in ((True, False), (False, False), (False, True)):
            with pytest.raises(InstallError, match="is a symbolic link") as caught:
                executor(dry_run=dry_run, force=force)[0].run([step])
            remove = shlex.join(["rm", "--", str(link)])
            assert f"remove the link with `{remove}` (what it points to stays)" in str(caught.value)
    assert snapshot(tmp_path) == before


STATE_QUERY = ("systemctl", "--user", "show", "--property=ActiveState", "--value", "x.service")
STOP_HINT = (
    "Nothing was removed. Stop it yourself with `systemctl --user disable --now x.service`, "
    "then rerun uninstall."
)


@pytest.mark.parametrize(
    ("states", "result", "proven"),
    [
        (("inactive", "failed"), done(0, "inactive\n"), True),
        (("inactive", "failed"), done(0, "failed\n"), True),
        # RefuseManualStop=yes, a restart loop, a stop still under way:
        (("inactive", "failed"), done(0, "active\n"), False),
        (("inactive", "failed"), done(0, "activating\n"), False),
        (("inactive", "failed"), done(0, "deactivating\n"), False),
        (("inactive", "failed"), done(1, "inactive\n", "Failed to connect to bus"), False),
        (("inactive", "failed"), OSError("systemctl is gone"), False),
    ],
)
def test_r7_rule3_check_stopped_proves_a_stop_or_stops_the_run(tmp_path, states, result, proven):
    # A failed `disable --now` was ignored and the removals went on. The
    # check's verdict, not the stop command's exit status, decides.
    unit = tmp_path / "x.service"
    unit.write_text(f"# {MARKER}\n")
    runner = Runner(result)
    run, out = executor(runner=runner)
    steps = [
        CheckStopped(STATE_QUERY, "Check that x.service is stopped", STOP_HINT, states=states),
        RemovePath(str(unit), "Remove x.service"),
    ]
    if proven:
        run.run(steps)
        assert not unit.exists() and "    ok: stopped" in out.getvalue()
    else:
        with pytest.raises(InstallError) as caught:
            run.run(steps)
        assert str(caught.value) == (
            f"Step 1 (Check that x.service is stopped) failed: not proven stopped. {STOP_HINT}"
        )
        assert unit.read_text() == f"# {MARKER}\n"
    ((argv, kwargs),) = runner.calls
    assert argv == list(STATE_QUERY) and kwargs["capture_output"] is True
    # What the query printed is never shown.
    if isinstance(result, subprocess.CompletedProcess):
        for text in (result.stdout, result.stderr):
            assert not text.strip() or text.strip() not in out.getvalue()


def test_r7_check_stopped_only_prints_in_a_dry_run():
    runner = Runner()
    run, out = executor(dry_run=True, runner=runner)
    run.run([CheckStopped(STATE_QUERY, "Check", STOP_HINT, states=("inactive",))])
    assert runner.calls == []
    text = out.getvalue()
    assert f"    $ {' '.join(STATE_QUERY)}\n    (checked when applied)\n" in text


def test_r7_tree_size_counts_regular_files_and_never_follows_links(tmp_path, monkeypatch):
    top = tmp_path / "venv"
    (top / "lib").mkdir(parents=True)
    (top / "lib" / "a").write_bytes(b"x" * 1000)
    (top / "b").write_bytes(b"y" * 24)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * 5000)
    symlink_or_skip(top / "linked", outside)
    probe = HostProbe(simulated("debian"))
    assert probe.tree_size(str(top)) == 1024
    assert probe.tree_size(str(top / "b")) == 24
    assert probe.tree_size(str(top / "linked")) is None
    assert probe.tree_size(str(tmp_path / "missing")) is None
    monkeypatch.setattr(probe_module, "MAX_SIZE_ENTRIES", 2)
    assert probe.tree_size(str(top)) is None


def test_a_logged_note_is_appended_owner_only_and_only_when_applied(tmp_path):
    log = tmp_path / "state" / "installer.log"
    note = Note("The previous one stays:\n    - /x: why", log=str(log))
    run, out = executor(dry_run=True)
    run.run([note])
    assert not log.exists() and f"when applied, this is also written to {log}" in out.getvalue()
    run, out = executor()
    run.run([note, note])
    assert log.read_text().count("The previous one stays:\n    - /x: why\n") == 2
    assert f"logged to {log}" in out.getvalue()
    if os.name != "nt":
        assert log.stat().st_mode & 0o777 == 0o600
        assert log.parent.stat().st_mode & 0o777 == 0o700


def test_wait_for_port_warns_instead_of_failing():
    sleeps = []
    run, out = executor(port_open=lambda port: False, sleep=sleeps.append)
    run.run([WaitForPort(8765, "port", timeout=2, hint="journalctl --user -u x")])
    assert len(sleeps) == 3
    assert "nothing listens on 127.0.0.1:8765 yet; see journalctl" in out.getvalue()
    ready, out = executor(port_open=lambda port: True)
    ready.run([WaitForPort(8765, "port")])
    assert "ok: listening" in out.getvalue() and not ready.warnings
